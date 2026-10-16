"""入金予定 × Moneytree 実績 → Chatwork / LINE WORKS 確認通知（サンプル）

  python main.py demo                     # 10/17〜10/22 を早送りで再現
  python main.py run [--now 2026-10-18T09:00]
  python main.py reply A001 "20日に振込予定です"   # モックChatworkで担当者の返信を再現
  python main.py status
  python main.py reset
  python main.py --service lineworks demo   # 通知先を LINE WORKS にして再現

通知先は config.json の notify_service（chatwork / lineworks）で選ぶ。

標準ライブラリのみ。config.json の mode=mock のあいだは外部に一切通信しない。
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
JST = dt.timezone(dt.timedelta(hours=9))
STATE_FILE = BASE / "data" / "state.json"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ---------------------------------------------------------------- util
def load_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def save_json(path, obj):
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))
    os.replace(tmp, p)


def parse_now(s):
    if not s:
        return dt.datetime.now(JST)
    d = dt.datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=JST)


def yen(n):
    return f"¥{int(n):,}"


def norm(s):
    """全角/半角カナ・記号のゆれを吸収して比較する。"""
    s = unicodedata.normalize("NFKC", s or "")
    for ch in " 　()（）.-ー・":
        s = s.replace(ch, "")
    return s.upper()


# ---------------------------------------------------------------- Moneytree
class MockMoneytree:
    """Moneytree の口座明細の代わり。now 以前の明細だけを返す。"""

    def __init__(self, cfg):
        self.rows = load_json(BASE / cfg["mock_file"], {"transactions": []})["transactions"]

    def deposits(self, now):
        today = now.date()
        return [t for t in self.rows
                if t["amount"] > 0 and dt.date.fromisoformat(t["date"]) <= today]


class CsvMoneytree:
    """Moneytree Business から書き出した明細CSVを読む。列名は config で合わせる。"""

    def __init__(self, cfg):
        self.path = BASE / cfg["csv_file"]
        self.cols = cfg["csv_columns"]

    def deposits(self, now):
        out = []
        with open(self.path, encoding="utf-8-sig", newline="") as f:
            for i, r in enumerate(csv.DictReader(f)):
                amt = int(str(r[self.cols["amount"]]).replace(",", "").replace("¥", "") or 0)
                d = dt.date.fromisoformat(r[self.cols["date"]].replace("/", "-"))
                if amt > 0 and d <= now.date():
                    out.append({"id": f"csv{i}", "date": d.isoformat(), "amount": amt,
                                "description": r[self.cols["description"]]})
        return out


# ---------------------------------------------------------------- Chatwork
class MockChatwork:
    """Chatwork の代わり。投稿は data/mock_chatwork.json に溜まる。"""

    def __init__(self, cfg):
        self.path = BASE / cfg["mock_file"]
        self.bot = str(cfg["bot_account_id"])

    def _load(self):
        return load_json(self.path, {"next_id": 1, "messages": []})

    def post(self, room_id, body, now, account_id=None):
        d = self._load()
        mid = str(d["next_id"])
        d["next_id"] += 1
        d["messages"].append({"room_id": str(room_id), "message_id": mid,
                              "account_id": str(account_id or self.bot),
                              "send_time": int(now.timestamp()), "body": body})
        save_json(self.path, d)
        return mid

    def messages(self, room_id):
        return [m for m in self._load()["messages"] if m["room_id"] == str(room_id)]


class RealChatwork:
    API = "https://api.chatwork.com/v2"

    def __init__(self, cfg):
        self.token = os.environ.get(cfg["token_env"], "")
        if not self.token:
            sys.exit(f"環境変数 {cfg['token_env']} にChatworkのAPIトークンを入れてください")
        self.bot = str(cfg["bot_account_id"])

    def _req(self, method, path, data=None):
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(self.API + path, data=body, method=method,
                                     headers={"X-ChatWorkToken": self.token})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else []

    def post(self, room_id, body, now, account_id=None):
        return str(self._req("POST", f"/rooms/{room_id}/messages", {"body": body})["message_id"])

    def messages(self, room_id):
        rows = self._req("GET", f"/rooms/{room_id}/messages?force=1")  # 直近100件まで
        return [{"room_id": str(room_id), "message_id": str(m["message_id"]),
                 "account_id": str(m["account"]["account_id"]),
                 "send_time": m["send_time"], "body": m["body"]} for m in rows]


# ---------------------------------------------------------------- LINE WORKS
class MockLineWorks(MockChatwork):
    """LINE WORKS の代わり。投稿は data/mock_lineworks.json に溜まる。"""

    def __init__(self, cfg):
        self.path = BASE / cfg["mock_file"]
        self.bot = "bot"


class RealLineWorks:
    """LINE WORKS API 2.0 の Bot で送る。
    返信は LINE WORKS から受け口（Callback URL）へ届くので、受け口に貯めた分を読みに行く。
    受け口の例は lineworks_inbox_worker.js（Cloudflare Workers）。
    認証に RS256 の署名が要るため、本番だけ `pip install cryptography` が必要。"""
    AUTH = "https://auth.worksmobile.com/oauth2/v2.0/token"
    API = "https://www.worksapis.com/v1.0"

    def __init__(self, cfg):
        self.cfg = cfg
        self.bot = "bot"
        self._token = None
        self._seq = 0
        need = [cfg["client_secret_env"], cfg["private_key_env"], cfg["inbox_token_env"]]
        miss = [k for k in need if not os.environ.get(k)]
        if miss or not cfg.get("bot_id") or not cfg.get("inbox_url"):
            sys.exit("LINE WORKS の設定が足りません：環境変数 " + "・".join(miss or ["（なし）"])
                     + " / config の lineworks.bot_id・inbox_url を確認してください")

    def _jwt(self):
        import base64
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=")
        now = int(dt.datetime.now().timestamp())
        head = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        body = b64(json.dumps({"iss": self.cfg["client_id"], "sub": self.cfg["service_account"],
                               "iat": now, "exp": now + 3600}).encode())
        key = serialization.load_pem_private_key(os.environ[self.cfg["private_key_env"]].encode(), None)
        sig = b64(key.sign(head + b"." + body, padding.PKCS1v15(), hashes.SHA256()))
        return (head + b"." + body + b"." + sig).decode()

    def _access_token(self):
        if not self._token:
            data = urllib.parse.urlencode({
                "assertion": self._jwt(), "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "client_id": self.cfg["client_id"], "client_secret": os.environ[self.cfg["client_secret_env"]],
                "scope": "bot"}).encode()
            with urllib.request.urlopen(urllib.request.Request(self.AUTH, data=data), timeout=20) as r:
                self._token = json.loads(r.read())["access_token"]
        return self._token

    def post(self, room_id, body, now, account_id=None):
        url = f"{self.API}/bots/{self.cfg['bot_id']}/channels/{room_id}/messages"
        data = json.dumps({"content": {"type": "text", "text": body[:2000]}}).encode()
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": "Bearer " + self._access_token(), "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).close()
        # LINE WORKS は送信したメッセージのIDを返さないので、こちらで番号を振る
        self._seq += 1
        return f"lw{int(now.timestamp())}-{self._seq}"

    def messages(self, room_id):
        url = self.cfg["inbox_url"].rstrip("/") + "/messages?channel=" + urllib.parse.quote(str(room_id))
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + os.environ[self.cfg["inbox_token_env"]]})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())


SERVICES = {"chatwork": "Chatwork", "lineworks": "LINE WORKS"}


def service(cfg):
    s = cfg.get("notify_service", "chatwork")
    if s not in SERVICES:
        sys.exit(f"notify_service は {' / '.join(SERVICES)} のどちらかにしてください（いま：{s}）")
    return s


def make_chat(cfg, mock=None):
    s = service(cfg)
    c = cfg[s]
    use_mock = c["mode"] == "mock" if mock is None else mock
    if s == "lineworks":
        return MockLineWorks(c) if use_mock else RealLineWorks(c)
    return MockChatwork(c) if use_mock else RealChatwork(c)


def default_room(cfg):
    c = cfg[service(cfg)]
    return str(c.get("channel_id") or c.get("room_id") or "")


# ---------------------------------------------------------------- 本体
def load_expected(cfg):
    """担当者IDやルームIDが空の行は、設定画面の担当者一覧・既定ルームで補う。
    LINE WORKS のときは担当者一覧の lineworks_user_id と既定のトークルームを使う。"""
    lw = service(cfg) == "lineworks"
    key = "lineworks_user_id" if lw else "account_id"
    people = {a["name"]: a.get(key, "") for a in cfg.get("assignees", [])}
    room = default_room(cfg)
    with open(BASE / cfg["expected_file"], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r.setdefault("お客様", r.get("取引先", ""))
        r.setdefault("物件", "")
        r.setdefault("区分", "")
        if lw:
            r["担当者ID"] = people.get(r["担当者名"], "") or r.get("LW担当者ID", "")
            r["ルームID"] = r.get("LWチャンネルID") or room
        else:
            r["担当者ID"] = r.get("担当者ID") or people.get(r["担当者名"], "")
            r["ルームID"] = r.get("ルームID") or room
    return rows


def manager(cfg):
    m = cfg["manager"]
    if service(cfg) == "lineworks":
        return {"name": m["name"], "account_id": m.get("lineworks_user_id", "")}
    return m


def find_deposit(item, deposits, used, rules):
    due = dt.date.fromisoformat(item["入金予定日"])
    lo = due - dt.timedelta(days=rules["match_window_days_before"])
    hi = due + dt.timedelta(days=rules["match_window_days_after"])
    keys = [norm(k) for k in item["振込名義"].replace("|", "/").split("/") if k.strip()]
    hit, partial, same_amount = None, [], []
    for t in deposits:
        if t["id"] in used or not (lo <= dt.date.fromisoformat(t["date"]) <= hi):
            continue
        name_ok = any(k in norm(t["description"]) for k in keys)
        amount_ok = int(t["amount"]) == int(item["金額"])
        # 振込名義に登録していない家族（奥様・ご主人など）でも、苗字と金額が合えば同じお客様とみなす
        surname_ok = rules.get("match_surname_with_amount", True) and any(
            norm(k.split()[0]) in norm(t["description"]) for k in item["振込名義"].replace("|", "/").split("/") if k.strip())
        if amount_ok and (name_ok or surname_ok):
            hit = dict(t, match="名義" if name_ok else "苗字と金額")
            break
        if name_ok:
            partial.append(t)          # 本人名義だが金額が違う（分割・一部入金など）
        elif amount_ok:
            same_amount.append(t)      # 同額だが登録外の名義（親・会社名義など）
    return hit, partial + same_amount




def clean_text(body, item_id=None):
    """返信の本文から [rp]・[To]・メンション・先頭の案件番号を外して中身だけにする。"""
    body = re.sub(r"\[(rp|To|To:)[^\]]*\][^\n]*\n?", "", body)
    body = re.sub(r'<m userId="[^"]*">', "", body).strip()
    if item_id:
        body = re.sub(r"^\s*" + re.escape(item_id) + r"[\s:：、,.]*", "", body, flags=re.I)
    return body.strip()


def names_item(body, item_id, all_ids):
    """LINE WORKS には返信ボタンがないので、本文の先頭に案件番号があればその案件への返事とみなす。"""
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9-]*)", unicodedata.normalize("NFKC", clean_text(body)))
    return bool(m) and m.group(1).upper() == item_id.upper() and item_id in all_ids


def find_reply(chat, item, ids, since, owner_only_one, all_ids=()):
    """送ったメッセージ（ids）への返信を探す。
    返信ボタン（Chatwork）か先頭の案件番号（LINE WORKS）でどの案件か分かる発言はそのまま拾う。
    どちらもない発言は、その担当者の確認待ちが1件だけのときに限り返信とみなす。"""
    room = item["ルームID"]
    ids = set(ids)
    for m in chat.messages(room):
        if m["message_id"] in ids or m["account_id"] == chat.bot or m["send_time"] <= since:
            continue
        is_rp = any(f"to={room}-{mid}]" in m["body"] for mid in ids)
        by_id = names_item(m["body"], item["id"], all_ids)
        # 別のメッセージへの返信（[rp]）や、別の案件番号で始まる発言は、ほかの案件の返事なので拾わない
        other = "[rp " in m["body"] or any(names_item(m["body"], x, all_ids) for x in all_ids if x != item["id"])
        by_owner = owner_only_one and m["account_id"] == str(item["担当者ID"]) and not other
        if is_rp or by_id or by_owner:
            return m
    return None


def parse_reason(text, reasons):
    """「2 上棟が11月にずれたため」のように番号で始まる返信から原因を読む。"""
    m = re.match(r"\s*(\d+)", unicodedata.normalize("NFKC", text))
    if m and 1 <= int(m.group(1)) <= len(reasons):
        return int(m.group(1)) - 1
    return None


DEFAULT_REASONS = ["住宅ローンの実行待ち・遅れ", "工程の遅れ（着工・上棟・引渡しのずれ）",
                   "建築確認・各種申請の遅れ", "追加工事・金額変更の精算待ち",
                   "お客様の振込忘れ・手続き遅れ", "分割で入金予定",
                   "名義違いで入金済み（照合のみ）", "その他"]
REASONS = DEFAULT_REASONS

DEFAULT_TEMPLATES = {
    "ask": {"title": "入金予定の確認",
            "body": "お客様：{お客様}（{物件}）\n区分：{区分}\n金額：{金額}\n入金予定日：{予定日}（{残り}）\n\n"
                    "Moneytree上ではまだ入金を確認できていません。\n入金は大丈夫そうですか？ このメッセージに返信してください。"},
    "remind": {"title": "入金予定の確認（再送{再送回数}回目・未返信）",
               "body": "お客様：{お客様}（{物件}）\n区分：{区分}\n金額：{金額}\n入金予定日：{予定日}（{残り}）\n\n"
                       "まだ返信をいただけていません。入金は大丈夫そうですか？ このメッセージに返信してください。"},
    "paid": {"title": "入金を確認しました{予定日前}",
             "body": "お客様：{お客様}（{物件}）\n区分：{区分}\n金額：{入金額}\n入金日：{入金日}（予定 {予定日}）\n明細：{明細}{照合メモ}"},
    "overdue": {"title": "未入金アラート",
                "body": "お客様：{お客様}（{物件}）\n区分：{区分}\n金額：{金額}\n入金予定日：{予定日}（予定日を過ぎても入金を確認できません）"
                        "{参考の入金}\n\n担当者の返信：{返信}\n\n"
                        "遅れている原因を番号で返信してください（例：2 上棟が11月にずれたため）\n{原因の選択肢}"},
}
TEMPLATES = DEFAULT_TEMPLATES


CHAT_KIND = "chatwork"


def to_tag(aid, name):
    if CHAT_KIND == "lineworks":
        return f'<m userId="{aid}">さん' if aid else f"{name}さん"
    return f"[To:{aid}]{name}さん"


def fill(kind, v, to):
    t = TEMPLATES.get(kind, DEFAULT_TEMPLATES[kind])
    title, body = t["title"], t["body"]
    for k, val in v.items():
        title = title.replace("{" + k + "}", str(val))
        body = body.replace("{" + k + "}", str(val))
    head = "\n".join(to_tag(a, n) for a, n in to)
    if CHAT_KIND == "lineworks":
        # LINE WORKS には枠や見出しの書式がないので【】で見出しにし、返信に使う案件番号を添える
        foot = "" if kind == "paid" else f"\n\n案件番号：{v['案件番号']}（返信の先頭にこの番号を書いてください）"
        return f"{head}\n【{title}】\n{body}{foot}"
    return head + f"\n[info][title]{title}[/title]{body}[/info]"


def base_vars(item, days_left=0):
    when = f"あと{days_left}日" if days_left > 0 else ("本日" if days_left == 0 else f"{-days_left}日経過")
    return {"お客様": item["お客様"], "物件": item["物件"], "区分": item["区分"],
            "取引先": item["お客様"], "金額": yen(item["金額"]), "予定日": item["入金予定日"],
            "残り": when, "担当者": item["担当者名"], "案件番号": item["id"]}


def msg_ask(item, days_left, n=0, cc=None):
    to = [(item["担当者ID"], item["担当者名"])] + ([(cc["account_id"], cc["name"])] if cc else [])
    return fill("remind" if n else "ask", {**base_vars(item, days_left), "再送回数": n}, to)


def msg_ok(item, t, early):
    v = {**base_vars(item), "入金額": yen(t["amount"]), "入金日": t["date"],
         "明細": t["description"], "予定日前": "（予定日前）" if early else "",
         "照合メモ": "\n※振込名義に登録していない名前のため、苗字と金額で照合しました。念のため確認してください"
                     if t.get("match") == "苗字と金額" else ""}
    return fill("paid", v, [(item["担当者ID"], item["担当者名"])])


def msg_overdue(item, mgr, partial, reply):
    diff = ("\n\n※この入金ではないか確認してください（名義か金額のどちらかだけ一致）：\n" + "\n".join(
        f"・{p['date']} {yen(p['amount'])} {p['description']}" for p in partial)) if partial else ""
    v = {**base_vars(item), "参考の入金": diff, "金額違い": diff, "返信": reply or "なし",
         "原因の選択肢": "\n".join(f"{k + 1} {r}" for k, r in enumerate(REASONS))}
    return fill("overdue", v, [(item["担当者ID"], item["担当者名"]), (mgr["account_id"], mgr["name"])])


def in_send_hours(cfg, now):
    h = cfg["rules"].get("send_hours")
    return not h or h[0] <= now.hour < h[1]


def apply_cfg(cfg):
    global TEMPLATES, REASONS, CHAT_KIND
    CHAT_KIND = service(cfg)
    TEMPLATES = {**DEFAULT_TEMPLATES, **cfg.get("templates", {})}
    REASONS = cfg.get("delay_reasons") or DEFAULT_REASONS


def new_state():
    return {"status": "pending", "sent_ids": [], "reminders": 0, "first_sent_at": None,
            "last_sent_at": None, "reply": None, "deposit": None, "overdue_sent": False,
            "overdue_id": None, "overdue_at": None, "reason": None, "reason_text": None}


def run(cfg, now, quiet=False):
    apply_cfg(cfg)
    rules, mgr = cfg["rules"], manager(cfg)
    if not in_send_hours(cfg, now):
        if not quiet:
            print(f"[{now:%Y-%m-%d %H:%M}] 送信時間外のため何もしません")
        return []
    mt = (MockMoneytree if cfg["moneytree"]["mode"] == "mock" else CsvMoneytree)(cfg["moneytree"])
    chat = make_chat(cfg)
    state = load_json(STATE_FILE, {})
    deposits = mt.deposits(now)
    used = {s["deposit"]["id"] for s in state.values() if s.get("deposit")}
    items = load_expected(cfg)
    all_ids = {it["id"] for it in items}
    svc = SERVICES[service(cfg)]
    log = []
    waiting = {}
    for it in items:
        s = state.get(it["id"], {})
        if s.get("status") == "asked" or (s.get("status") == "overdue" and s.get("reason") is None):
            waiting[it["担当者ID"]] = waiting.get(it["担当者ID"], 0) + 1

    def send(item, body):
        mid = chat.post(item["ルームID"], body, now)
        log.append(f"  → {svc}送信 #{mid}  {item['id']} {item['お客様']} {item['区分']}")
        return mid

    for item in items:
        st = state.setdefault(item["id"], new_state())
        for k, v in new_state().items():
            st.setdefault(k, v)
        if st["status"] == "paid":
            continue
        due = dt.date.fromisoformat(item["入金予定日"])
        days_left = (due - now.date()).days
        only_one = waiting.get(item["担当者ID"], 0) == 1

        # 0) 予定表で入金済みにしてある行（システム導入前の入金など）は何も送らない
        if item.get("入金済日"):
            st.update(status="paid", deposit={"id": f"manual-{item['id']}", "date": item["入金済日"],
                                              "amount": int(item["金額"]), "description": "予定表で入金済"})
            continue

        # 1) 入金済みなら確認の連絡を出して終わり（導入前の古い入金は黙って入金済みにする）
        hit, partial = find_deposit(item, deposits, used, rules)
        if hit:
            used.add(hit["id"])
            st.update(status="paid", deposit=hit)
            age = (now.date() - dt.date.fromisoformat(hit["date"])).days
            if st["first_sent_at"] or age <= rules.get("paid_notice_within_days", 3):
                send(item, msg_ok(item, hit, early=dt.date.fromisoformat(hit["date"]) < due))
            continue

        # 2) 確認への返信が来ていれば記録（以後の再送は止める）
        if st["first_sent_at"] and not st["reply"]:
            r = find_reply(chat, item, st["sent_ids"], st["first_sent_at"] - 1, only_one, all_ids)
            if r:
                st["reply"] = clean_text(r["body"], item["id"])[:80]
                st["status"] = "replied" if st["status"] == "asked" else st["status"]
                log.append(f"  ← 返信あり  {item['id']}：{st['reply']}")

        # 3) 未入金アラートへの返信から遅れの原因を読む
        if st["overdue_sent"] and st["reason"] is None:
            r = find_reply(chat, item, [st["overdue_id"]], st["overdue_at"] - 1, only_one, all_ids)
            if r:
                txt = clean_text(r["body"], item["id"])
                n = parse_reason(txt, REASONS)
                if n is not None:
                    st["reason"], st["reason_text"] = n, txt[:80]
                    log.append(f"  ← 原因を記録  {item['id']}：{REASONS[n]}（{txt[:40]}）")

        # 4) 予定日のN日前になったら確認を送る（すでに大きく遅れているものはアラートだけにする）
        if (st["first_sent_at"] is None and -rules["overdue_grace_days"] <= days_left
                <= rules["ask_days_before"]):
            st["sent_ids"].append(send(item, msg_ask(item, days_left)))
            st["first_sent_at"] = st["last_sent_at"] = int(now.timestamp())
            st["status"] = "asked"

        # 5) 返信がないまま一定時間たったら再送（回数上限あり・途中から上長もTo）
        elif (st["status"] == "asked" and st["reminders"] < rules["max_reminders"]
              and now.timestamp() - st["last_sent_at"] >= rules["remind_after_hours"] * 3600):
            st["reminders"] += 1
            cc = mgr if st["reminders"] >= rules["cc_manager_from_reminder"] else None
            st["sent_ids"].append(send(item, msg_ask(item, days_left, st["reminders"], cc)))
            st["last_sent_at"] = int(now.timestamp())

        # 6) 予定日＋猶予を過ぎても入金がなければ上長へアラート（1回）
        if days_left < -rules["overdue_grace_days"] and not st["overdue_sent"]:
            mid = send(item, msg_overdue(item, mgr, partial, st["reply"]))
            st.update(overdue_sent=True, overdue_id=mid, overdue_at=int(now.timestamp()), status="overdue")

    save_json(STATE_FILE, state)
    if not quiet:
        print(f"[{now:%Y-%m-%d %H:%M}] 実行")
        print("\n".join(log) if log else "  （送信なし）")
    return log


# ---------------------------------------------------------------- 入金進捗の集計
def man(n):
    return f"{round(n / 10000):,}万円"


def month_add(y, m, k):
    m += k
    while m > 12:
        m, y = m - 12, y + 1
    return y, m


def progress_rows(cfg, now):
    """予定表の全行について、入金済・期限超過・これから を判定する。"""
    apply_cfg(cfg)
    state = load_json(STATE_FILE, {})
    mt = (MockMoneytree if cfg["moneytree"]["mode"] == "mock" else CsvMoneytree)(cfg["moneytree"])
    deposits = mt.deposits(now)
    used = {s["deposit"]["id"] for s in state.values() if s.get("deposit")}
    rows = []
    for item in load_expected(cfg):
        st = state.get(item["id"], {})
        amt = int(item["金額"])
        paid_amt, paid_date = 0, None
        if st.get("deposit"):
            paid_amt, paid_date = int(st["deposit"]["amount"]), st["deposit"]["date"]
        elif item.get("入金済日"):
            paid_amt, paid_date = amt, item["入金済日"]
        else:
            hit, _ = find_deposit(item, deposits, used, cfg["rules"])
            if hit:
                used.add(hit["id"])
                paid_amt, paid_date = int(hit["amount"]), hit["date"]
        due = dt.date.fromisoformat(item["入金予定日"])
        status = "paid" if paid_amt else ("overdue" if due < now.date() else "plan")
        reason = st.get("reason")
        rows.append({**item, "amt": amt, "paid": paid_amt, "paid_date": paid_date, "due": due,
                     "status": status, "late_days": (now.date() - due).days,
                     "reason": REASONS[reason] if reason is not None and reason < len(REASONS) else None,
                     "reason_text": st.get("reason_text")})
    return rows


def summarize(rows):
    plan = sum(r["amt"] for r in rows)
    paid = sum(r["paid"] for r in rows)
    late = sum(r["amt"] - r["paid"] for r in rows if r["status"] == "overdue")
    return {"plan": plan, "paid": paid, "late": late, "rest": plan - paid - late,
            "pct": paid / plan if plan else 0, "n": len(rows),
            "n_paid": sum(r["status"] == "paid" for r in rows)}


def cmd_progress(cfg, now, csv_out=False):
    rows = progress_rows(cfg, now)
    fy = int(cfg["rules"].get("fiscal_year_start_month", 4))
    y = now.year if now.month >= fy else now.year - 1
    months = [month_add(y, fy, k) for k in range(12)]
    in_fy = [r for r in rows if (r["due"].year, r["due"].month) in months]
    this = [r for r in rows if (r["due"].year, r["due"].month) == (now.year, now.month)]

    def line(title, s):
        print(f"{title}\n  入金済 {man(s['paid'])} / 予定 {man(s['plan'])}（{s['pct']:.0%}）"
              f"  これから {man(s['rest'])}  期限超過・未入金 {man(s['late'])}"
              f"  ［{s['n']}件中 {s['n_paid']}件入金済］")

    print(f"================ 入金進捗（{now:%Y-%m-%d %H:%M} 時点）================")
    line(f"■ 今月（{now.month}月）", summarize(this))
    line(f"■ 年間（{months[0][0]}年{months[0][1]}月〜{months[-1][0]}年{months[-1][1]}月）", summarize(in_fy))

    print("\n■ 月別")
    print(f"  {'月':<6}{'予定':>12}{'入金済':>12}{'期限超過':>12}{'進捗':>7}")
    per = []
    for (yy, mm) in months:
        s = summarize([r for r in in_fy if (r["due"].year, r["due"].month) == (yy, mm)])
        per.append(((yy, mm), s))
        mark = " ←今月" if (yy, mm) == (now.year, now.month) else ""
        print(f"  {str(mm) + '月':<6}{man(s['plan']):>12}{man(s['paid']):>12}"
              f"{(man(s['late']) if s['late'] else '—'):>12}{(f'{s['pct']:.0%}' if s['plan'] else '—'):>7}{mark}")

    late = sorted((r for r in in_fy if r["status"] == "overdue"), key=lambda r: r["due"])
    print("\n■ 遅れている入金と原因")
    if not late:
        print("  期限を過ぎた未入金はありません")
    for r in late:
        why = r["reason"] or "原因が未記録"
        note = f"（{r['reason_text']}）" if r["reason_text"] else ""
        print(f"  {r['id']} {r['お客様']} {r['区分']} {yen(r['amt'] - r['paid'])}  "
              f"{r['late_days']}日遅れ  担当 {r['担当者名']}\n      → {why}{note}")

    print("\n■ 原因別の金額（年間）")
    agg = {}
    for r in late:
        k = r["reason"] or "原因が未記録"
        agg[k] = agg.get(k, 0) + r["amt"] - r["paid"]
    for k, v in sorted(agg.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<28} {man(v):>10}")

    if csv_out:
        out = BASE / "data" / f"progress_{now:%Y%m%d}.csv"
        with open(out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["id", "お客様", "物件", "区分", "担当者", "入金予定日", "予定額", "入金額",
                        "入金日", "状態", "遅れ日数", "遅れの原因", "担当者のコメント"])
            label = {"paid": "入金済", "overdue": "期限超過", "plan": "これから"}
            for r in sorted(rows, key=lambda r: r["due"]):
                w.writerow([r["id"], r["お客様"], r["物件"], r["区分"], r["担当者名"], r["due"],
                            r["amt"], r["paid"], r["paid_date"] or "", label[r["status"]],
                            r["late_days"] if r["status"] == "overdue" else "",
                            r["reason"] or "", r["reason_text"] or ""])
        print(f"\nCSVに書き出しました：{out}")


def cmd_reason(cfg, item_id, n, note):
    """遅れの原因を手で記録する（画面のプルダウンにあたる）。"""
    apply_cfg(cfg)
    state = load_json(STATE_FILE, {})
    if not 1 <= n <= len(REASONS):
        sys.exit(f"原因の番号は 1〜{len(REASONS)} で指定してください：\n" +
                 "\n".join(f"  {k + 1} {r}" for k, r in enumerate(REASONS)))
    st = state.setdefault(item_id, new_state())
    st["reason"], st["reason_text"] = n - 1, note or None
    save_json(STATE_FILE, state)
    print(f"{item_id} の遅れの原因を「{REASONS[n - 1]}」にしました")


def cmd_status(cfg):
    apply_cfg(cfg)
    state = load_json(STATE_FILE, {})
    label = {"pending": "未通知", "asked": "確認中・未返信", "replied": "返信あり",
             "paid": "入金済", "overdue": "未入金アラート済"}
    for item in load_expected(cfg):
        st = state.get(item["id"], {})
        if st.get("status", "pending") == "pending" or (
                st.get("status") == "paid" and not st.get("sent_ids") and not item["id"].startswith("A")):
            continue
        why = st.get("reason")
        print(f"{item['id']} {item['お客様']:<10}{item['区分']:<4} {yen(item['金額']):>12} 予定{item['入金予定日']}  "
              f"{label.get(st.get('status', 'pending'))}  再送{st.get('reminders', 0)}回"
              + (f"  返信「{st['reply']}」" if st.get("reply") else "")
              + (f"  原因：{REASONS[why]}" if why is not None else ""))


def cmd_reply(cfg, item_id, text, now):
    """モック用：担当者が直近のBotメッセージに返信した状態を作る。
    Chatwork は返信ボタン、LINE WORKS は先頭に案件番号を書いた発言として作る。"""
    chat = make_chat(cfg, mock=True)
    item = next(i for i in load_expected(cfg) if i["id"] == item_id)
    st = load_json(STATE_FILE, {}).get(item_id)
    if not st or not (st.get("sent_ids") or st.get("overdue_id")):
        sys.exit("まだ確認メッセージを送っていません")
    room = item["ルームID"]
    mid = st.get("overdue_id") or st["sent_ids"][-1]
    if service(cfg) == "lineworks":
        body = f"{item_id} {text}"
    else:
        body = f"[rp aid={chat.bot} to={room}-{mid}]確認Botさん\n{text}"
    chat.post(room, body, now, account_id=item["担当者ID"])
    print(f"[{now:%Y-%m-%d %H:%M}] {item['担当者名']}さんが返信：{text}")


def cmd_reset(cfg):
    for p in [STATE_FILE] + [BASE / cfg[k]["mock_file"] for k in SERVICES if k in cfg]:
        if Path(p).exists():
            Path(p).unlink()
    print("状態とモックのチャット履歴を消しました")


def cmd_demo(cfg):
    if cfg[service(cfg)]["mode"] != "mock" or cfg["moneytree"]["mode"] != "mock":
        sys.exit("demo は mode=mock のときだけ実行できます")
    cmd_reset(cfg)
    steps = [
        ("run", "2026-10-16T09:00"),
        ("reply", "2026-10-16T13:00", "P04-3", "2 上棟が9月末までずれ込み、中間金の請求も遅れています"),
        ("run", "2026-10-17T09:00"),
        ("reply", "2026-10-17T11:30", "A001", "お客様に確認済みです。ローン実行が20日なので当日に振り込まれます"),
        ("run", "2026-10-18T09:00"),
        ("run", "2026-10-19T09:00"),
        ("run", "2026-10-20T18:00"),
        ("run", "2026-10-22T09:00"),
        ("reply", "2026-10-22T10:15", "P05-2", "３ 建築確認の補正で着工が遅れ、着工金の請求も止まっています"),
        ("run", "2026-10-22T11:00"),
    ]
    for s in steps:
        print()
        if s[0] == "run":
            run(cfg, parse_now(s[1]))
        else:
            cmd_reply(cfg, s[2], s[3], parse_now(s[1]))
    print("\n================ 通知した案件の状態 ================")
    cmd_status(cfg)
    print()
    cmd_progress(cfg, parse_now("2026-10-22T11:00"), csv_out=True)
    print("\n================ 未入金アラートの文面（例） ================")
    msgs = make_chat(cfg, mock=True).messages(default_room(cfg))
    m = [x for x in msgs if "未入金アラート" in x["body"]][-1]
    print(m["body"])


def main():
    cfg = load_json(BASE / "config.json", None)
    ap = argparse.ArgumentParser()
    ap.add_argument("--service", choices=list(SERVICES), help="config の notify_service を一時的に切り替える")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run"); p.add_argument("--now")
    p = sub.add_parser("reply"); p.add_argument("id"); p.add_argument("text"); p.add_argument("--now")
    p = sub.add_parser("progress"); p.add_argument("--now"); p.add_argument("--csv", action="store_true")
    p = sub.add_parser("reason"); p.add_argument("id"); p.add_argument("number", type=int); p.add_argument("note", nargs="?")
    sub.add_parser("status"); sub.add_parser("reset"); sub.add_parser("demo")
    a = ap.parse_args()
    if a.service:
        cfg["notify_service"] = a.service
    apply_cfg(cfg)
    if a.cmd == "run":
        run(cfg, parse_now(a.now))
    elif a.cmd == "reply":
        cmd_reply(cfg, a.id, a.text, parse_now(a.now))
    elif a.cmd == "progress":
        cmd_progress(cfg, parse_now(a.now), a.csv)
    elif a.cmd == "reason":
        cmd_reason(cfg, a.id, a.number, a.note)
    elif a.cmd == "status":
        cmd_status(cfg)
    elif a.cmd == "reset":
        cmd_reset(cfg)
    else:
        cmd_demo(cfg)


if __name__ == "__main__":
    main()
