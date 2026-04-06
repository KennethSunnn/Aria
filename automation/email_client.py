"""
邮件收发客户端（email_send / email_read）

支持协议：
  发送：SMTP（SSL / STARTTLS / 明文）
  接收：IMAP（SSL）

环境变量配置（优先级高于 params）：
  ARIA_EMAIL_SMTP_HOST      SMTP 服务器地址
  ARIA_EMAIL_SMTP_PORT      SMTP 端口（默认 465）
  ARIA_EMAIL_SMTP_USER      SMTP 用户名
  ARIA_EMAIL_SMTP_PASS      SMTP 密码
  ARIA_EMAIL_SMTP_TLS       ssl/starttls/none（默认 ssl）
  ARIA_EMAIL_IMAP_HOST      IMAP 服务器地址
  ARIA_EMAIL_IMAP_PORT      IMAP 端口（默认 993）
  ARIA_EMAIL_IMAP_USER      IMAP 用户名
  ARIA_EMAIL_IMAP_PASS      IMAP 密码
  ARIA_EMAIL_FROM           发件人地址（默认同 SMTP_USER）
"""

from __future__ import annotations

import email as _email_lib
import imaplib
import logging
import os
import smtplib
import ssl
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# 配置读取                                                              #
# ------------------------------------------------------------------ #

def _smtp_cfg(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "host": str(params.get("smtp_host") or os.getenv("ARIA_EMAIL_SMTP_HOST", "")).strip(),
        "port": int(params.get("smtp_port") or os.getenv("ARIA_EMAIL_SMTP_PORT", "465")),
        "user": str(params.get("smtp_user") or os.getenv("ARIA_EMAIL_SMTP_USER", "")).strip(),
        "password": str(params.get("smtp_pass") or os.getenv("ARIA_EMAIL_SMTP_PASS", "")).strip(),
        "tls": str(params.get("smtp_tls") or os.getenv("ARIA_EMAIL_SMTP_TLS", "ssl")).strip().lower(),
        "from_addr": str(params.get("from_addr") or os.getenv("ARIA_EMAIL_FROM", "")).strip(),
    }


def _imap_cfg(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "host": str(params.get("imap_host") or os.getenv("ARIA_EMAIL_IMAP_HOST", "")).strip(),
        "port": int(params.get("imap_port") or os.getenv("ARIA_EMAIL_IMAP_PORT", "993")),
        "user": str(params.get("imap_user") or os.getenv("ARIA_EMAIL_IMAP_USER", "")).strip(),
        "password": str(params.get("imap_pass") or os.getenv("ARIA_EMAIL_IMAP_PASS", "")).strip(),
    }


# ------------------------------------------------------------------ #
# 工具函数                                                              #
# ------------------------------------------------------------------ #

def _decode_header_value(raw: str) -> str:
    """解码邮件头字段（处理 =?utf-8?...?= 编码）。"""
    parts = decode_header(raw or "")
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(str(part))
    return "".join(result)


def _get_body(msg) -> str:
    """提取邮件正文（优先 text/plain，回退 text/html）。"""
    if msg.is_multipart():
        plain = ""
        html = ""
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition") or "")
            if "attachment" in cd:
                continue
            charset = part.get_content_charset() or "utf-8"
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            text = payload.decode(charset, errors="replace")
            if ct == "text/plain" and not plain:
                plain = text
            elif ct == "text/html" and not html:
                html = text
        return plain or html
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        return payload.decode(charset, errors="replace") if payload else ""


# ------------------------------------------------------------------ #
# 发送邮件                                                              #
# ------------------------------------------------------------------ #

def email_send(params: dict[str, Any]) -> dict[str, Any]:
    """
    发送邮件。

    params:
        to          收件人（字符串或列表）
        subject     主题
        body        正文
        html        HTML 正文（可选，优先于 body）
        cc          抄送（字符串或列表，可选）
        smtp_host / smtp_port / smtp_user / smtp_pass / smtp_tls / from_addr
    """
    cfg = _smtp_cfg(params)

    if not cfg["host"]:
        return {"success": False, "message": "missing_smtp_host", "error_code": "config_error"}
    if not cfg["user"] or not cfg["password"]:
        return {"success": False, "message": "missing_smtp_credentials", "error_code": "config_error"}

    to_raw = params.get("to") or ""
    to_list = [t.strip() for t in (to_raw if isinstance(to_raw, list) else to_raw.split(",")) if t.strip()]
    if not to_list:
        return {"success": False, "message": "missing_recipient", "error_code": "param_error"}

    subject = str(params.get("subject") or "（无主题）").strip()
    html_body = str(params.get("html") or "").strip()
    text_body = str(params.get("body") or "").strip()
    from_addr = cfg["from_addr"] or cfg["user"]

    cc_raw = params.get("cc") or ""
    cc_list = [c.strip() for c in (cc_raw if isinstance(cc_raw, list) else cc_raw.split(",")) if c.strip()]

    # 构建 MIME
    if html_body:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(text_body or html_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        msg = MIMEText(text_body, "plain", "utf-8")

    msg["From"] = from_addr
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)

    all_recipients = to_list + cc_list

    try:
        tls = cfg["tls"]
        if tls == "ssl":
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ctx, timeout=15) as server:
                server.login(cfg["user"], cfg["password"])
                server.sendmail(from_addr, all_recipients, msg.as_string())
        elif tls == "starttls":
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(cfg["user"], cfg["password"])
                server.sendmail(from_addr, all_recipients, msg.as_string())
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as server:
                server.login(cfg["user"], cfg["password"])
                server.sendmail(from_addr, all_recipients, msg.as_string())

        return {
            "success": True,
            "message": f"邮件已发送至 {', '.join(to_list)}",
            "to": to_list,
            "subject": subject,
        }
    except smtplib.SMTPAuthenticationError as e:
        return {"success": False, "message": f"SMTP 认证失败：{e}", "error_code": "auth_error"}
    except smtplib.SMTPException as e:
        return {"success": False, "message": f"SMTP 错误：{e}", "error_code": "smtp_error"}
    except Exception as e:
        return {"success": False, "message": f"email_send_failed：{e}", "error_code": "unknown"}


# ------------------------------------------------------------------ #
# 读取邮件                                                              #
# ------------------------------------------------------------------ #

def email_read(params: dict[str, Any]) -> dict[str, Any]:
    """
    读取收件箱邮件。

    params:
        folder      邮箱文件夹（默认 INBOX）
        limit       最多返回条数（默认 10）
        unread_only 是否只读未读邮件（默认 False）
        imap_host / imap_port / imap_user / imap_pass
    """
    cfg = _imap_cfg(params)

    if not cfg["host"]:
        return {"success": False, "message": "missing_imap_host", "error_code": "config_error"}
    if not cfg["user"] or not cfg["password"]:
        return {"success": False, "message": "missing_imap_credentials", "error_code": "config_error"}

    folder = str(params.get("folder") or "INBOX").strip()
    limit = max(1, int(params.get("limit") or 10))
    unread_only = bool(params.get("unread_only", False))

    try:
        with imaplib.IMAP4_SSL(cfg["host"], cfg["port"]) as imap:
            imap.login(cfg["user"], cfg["password"])
            imap.select(folder, readonly=True)

            search_criteria = "UNSEEN" if unread_only else "ALL"
            status, data = imap.search(None, search_criteria)
            if status != "OK":
                return {"success": False, "message": f"IMAP search failed: {status}", "error_code": "imap_error"}

            ids = data[0].split()
            # 取最新的 limit 封
            ids = ids[-limit:][::-1]

            emails = []
            for uid in ids:
                try:
                    status2, msg_data = imap.fetch(uid, "(RFC822)")
                    if status2 != "OK" or not msg_data or not msg_data[0]:
                        continue
                    raw = msg_data[0][1]
                    msg = _email_lib.message_from_bytes(raw)
                    emails.append({
                        "uid": uid.decode(),
                        "from": _decode_header_value(msg.get("From", "")),
                        "to": _decode_header_value(msg.get("To", "")),
                        "subject": _decode_header_value(msg.get("Subject", "")),
                        "date": msg.get("Date", ""),
                        "body": _get_body(msg)[:2000],  # 截断避免过长
                    })
                except Exception as e:
                    logger.debug(f"email_read fetch uid={uid} error: {e}")
                    continue

            return {
                "success": True,
                "folder": folder,
                "count": len(emails),
                "emails": emails,
            }

    except imaplib.IMAP4.error as e:
        return {"success": False, "message": f"IMAP 错误：{e}", "error_code": "imap_error"}
    except Exception as e:
        return {"success": False, "message": f"email_read_failed：{e}", "error_code": "unknown"}
