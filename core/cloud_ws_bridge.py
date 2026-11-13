#!/usr/bin/env python3
"""
SVT Cloud WebSocket Bridge (`cloud_ws_bridge.py`)
=================================================
Runs on any external cloud VM with outbound WebSocket support (e.g., GitHub Actions `ubuntu-latest` runner
or Google Cloud `e2-micro` VPS).
1. Connects to Feishu Open Platform's official WebSocket long-connection (`card.action.trigger`) via `lark-cli`.
2. Keeps `stdin` open so `lark-cli event consume` stays online 24/7.
3. The instant a user submits a dynamic Card 2.0 form or clicks a button in Feishu:
   - Immediately updates the Feishu card in-place to green (`✅ 已收到您的提交`) via `/open-apis/interactive/v1/card/update` or `PATCH /open-apis/im/v1/messages/:message_id`;
   - Forwards the raw event JSON via HTTPS `PUT` to `https://ntfy.sh/svt_feishu_webhook_cli_aa643241e7b8dce2`
     so the Antigravity sandbox VM (`feishu_jewelry_waiter.py`) receives it over standard HTTPS!
"""
import json
import os
import subprocess
import sys
import time

APP_ID = os.environ.get("FEISHU_APP_ID", "cli_aa643241e7b8dce2")
PROFILE = os.environ.get("FEISHU_PROFILE", "jewelry_bot")
NTFY_RELAY_URL = os.environ.get("NTFY_RELAY_URL", "https://ntfy.sh/svt_feishu_webhook_cli_aa643241e7b8dce2")
LARK_CLI = os.environ.get("LARK_CLI_BIN", "lark-cli")


STATUS_URL = os.environ.get("NTFY_STATUS_URL", "https://ntfy.sh/svt_feishu_ws_status_cli_aa643241e7b8dce2")


def report_status(msg):
    print(f"[cloud_ws_bridge] {msg}", flush=True)
    try:
        subprocess.run(
            ["curl", "-s", "-X", "POST", STATUS_URL, "-d", str(msg)[:1500]],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        pass


def setup_lark_cli_if_needed():
    secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not secret:
        report_status("WARNING: FEISHU_APP_SECRET is empty!")
        return
    report_status(f"configuring lark-cli profile={PROFILE} app_id={APP_ID}...")
    proc = subprocess.Popen(
        [
            LARK_CLI,
            "config",
            "init",
            "--name",
            PROFILE,
            "--app-id",
            APP_ID,
            "--app-secret-stdin",
            "--brand",
            "feishu",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    out, _ = proc.communicate(input=secret + "\n", timeout=15)
    report_status(f"config init rc={proc.returncode} output: {out.strip()}")


def build_quick_green_card(topic, form_val, act_val):
    items = []
    is_form = bool(form_val and isinstance(form_val, dict))
    if is_form:
        for k, v in form_val.items():
            if isinstance(v, list):
                val_s = "、".join(str(x) for x in v) if v else "（未选）"
            else:
                val_s = str(v).strip() if v is not None and str(v).strip() != "" else "（未填）"
            items.append((str(k), val_s))
    else:
        btn_txt = (act_val or {}).get("button_text") or (act_val or {}).get("intent") or "由 AI 自由发挥"
        items.append(("点选动作", f"`{btn_txt}`"))

    origin_wm = str((act_val or {}).get("card_width_mode") or "").strip().lower()
    if len(items) >= 3 or (origin_wm == "fill" and is_form and len(items) >= 2):
        width_mode = "fill"
    elif origin_wm in ("default", "compact"):
        width_mode = origin_wm
    else:
        width_mode = "default"

    elements = [{"tag": "markdown", "content": "**已确认回传内容**"}]
    if len(items) >= 2:
        step = min(len(items), 4 if width_mode == "fill" else 2)
        for idx in range(0, len(items), step):
            chunk = items[idx : idx + step]
            flex = "bisect" if len(chunk) >= 2 else "none"
            cols = [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "top",
                    "background_style": "grey",
                    "padding": "6px 10px 6px 10px",
                    "elements": [{"tag": "markdown", "content": f"**{k}**\n{v}", "text_size": "notation"}],
                }
                for k, v in chunk
            ]
            elements.append({"tag": "column_set", "flex_mode": flex, "horizontal_spacing": "10px", "margin": "4px 0px", "columns": cols})
    else:
        k, v = items[0]
        elements.append({"tag": "markdown", "content": f"• **{k}**：{v}"})

    return {
        "schema": "2.0",
        "config": {"width_mode": width_mode, "update_multi": True, "enable_forward": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"✅ 已锁定您的选择 · {topic}"},
            "subtitle": {"tag": "plain_text", "content": "反重力珠宝匠已收到回传信息，正在为您执行…"},
            "template": "green",
            "text_tag_list": [
                {"tag": "text_tag", "text": {"tag": "plain_text", "content": "已提交"}, "color": "green"},
            ],
        },
        "body": {"elements": elements},
    }


def handle_ws_event(ev):
    ev_id = ev.get("event_id") or f"ws_{int(time.time()*1000)}"
    msg_id = ev.get("message_id") or ""
    cid = ev.get("chat_id") or ""
    token = ev.get("token") or ""
    op_id = ev.get("operator_id") or ""

    form_val = {}
    if ev.get("form_value"):
        try:
            form_val = json.loads(ev["form_value"]) if isinstance(ev["form_value"], str) else ev["form_value"]
        except Exception:
            form_val = {"raw": ev.get("form_value")}

    act_val = {}
    if ev.get("action_value"):
        try:
            act_val = json.loads(ev["action_value"]) if isinstance(ev["action_value"], str) else ev["action_value"]
        except Exception:
            act_val = {"raw": ev.get("action_value")}

    topic = act_val.get("topic") or "互动确认"

    # 1. Update card in-place immediately (<0.3s)
    green_card = build_quick_green_card(topic, form_val, act_val)
    if token:
        subprocess.run(
            [
                LARK_CLI,
                "--profile",
                PROFILE,
                "api",
                "POST",
                "/open-apis/interactive/v1/card/update",
                "--as",
                "bot",
                "--data",
                json.dumps({"token": token, "card": green_card}, ensure_ascii=False),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    if msg_id:
        subprocess.run(
            [
                LARK_CLI,
                "--profile",
                PROFILE,
                "api",
                "PATCH",
                f"/open-apis/im/v1/messages/{msg_id}",
                "--as",
                "bot",
                "--data",
                json.dumps({"content": json.dumps(green_card, ensure_ascii=False)}, ensure_ascii=False),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

    # 2. Forward normalized event to Antigravity VM via HTTPS PUT
    normalized = {
        "schema": "2.0",
        "header": {"event_id": ev_id, "event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": op_id},
            "token": token,
            "context": {"open_message_id": msg_id, "open_chat_id": cid},
            "action": {
                "tag": ev.get("action_tag") or "button",
                "name": ev.get("action_name") or "",
                "form_value": form_val,
                "value": act_val,
            },
        },
    }
    subprocess.run(
        [
            "curl",
            "-s",
            "-X",
            "PUT",
            NTFY_RELAY_URL,
            "-H",
            "Filename: feishu_ws_event.json",
            "-d",
            json.dumps(normalized, ensure_ascii=False),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    report_status(f"forwarded event_id={ev_id} msg_id={msg_id}")


def main():
    setup_lark_cli_if_needed()
    max_run_sec = int(os.environ.get("MAX_RUN_SECONDS", "19800"))  # 5.5 hours default
    start_ts = time.time()

    env = os.environ.copy()
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"

    while time.time() - start_ts < max_run_sec:
        rem = int(max_run_sec - (time.time() - start_ts))
        if rem <= 10:
            break
        cmd = [
            LARK_CLI,
            "--profile",
            PROFILE,
            "event",
            "consume",
            "card.action.trigger",
            "--as",
            "bot",
            "--timeout",
            f"{rem}s",
        ]
        report_status(f"starting lark-cli WebSocket consumer (remaining={rem}s)...")
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        _keep_stdin = proc.stdin
        for line in proc.stdout:
            line_s = line.strip()
            if not line_s:
                continue
            print(f"[lark-cli] {line_s}", flush=True)
            if "[event] ready" in line_s:
                time.sleep(2)
                bus_log_path = os.path.expanduser(f"~/.lark-cli/events/{APP_ID}/bus.log")
                bus_txt = ""
                if os.path.exists(bus_log_path):
                    try:
                        bus_txt = open(bus_log_path, "r", encoding="utf-8", errors="ignore").read()[-600:]
                    except Exception:
                        pass
                report_status(f"READY: {line_s} | bus.log: {bus_txt}")
            elif line_s.startswith("{"):
                try:
                    ev = json.loads(line_s)
                    if ev.get("type") == "card.action.trigger" or "action_tag" in ev:
                        handle_ws_event(ev)
                    else:
                        report_status(f"lark-cli json: {line_s[:400]}")
                except Exception as e:
                    report_status(f"parse warning: {e}")
        proc.wait(timeout=5)
        time.sleep(2)


if __name__ == "__main__":
    main()
