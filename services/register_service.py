from __future__ import annotations

import json
import threading
import time
import uuid
from collections import defaultdict
from contextlib import ExitStack
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

from services.account_service import account_service
from services.config import DATA_DIR
from services.register import openai_register
from services.proxy_pool_service import load_available_proxies, proxy_by_key, proxy_url


REGISTER_FILE = DATA_DIR / "register.json"
REGISTER_LOG_DIR = DATA_DIR / "register_logs"
REGISTER_PROVIDER_CONCURRENCY = 2
REGISTER_DOMAIN_CONCURRENCY = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_config() -> dict:
    return {
        **openai_register.config,
        "mode": "total",
        "target_quota": 100,
        "target_available": 10,
        "check_interval": 5,
        "add_to_local_pool": True,
        "enabled": False,
        "registered_accounts": [],
        "deleted_registered_emails": [],
        "stats": {
            "success": 0,
            "fail": 0,
            "done": 0,
            "running": 0,
            "threads": openai_register.config["threads"],
            "elapsed_seconds": 0,
            "avg_seconds": 0,
            "success_rate": 0,
            "current_quota": 0,
            "current_available": 0,
        },
    }


def _mail_provider_ref(provider_type: str, index: int) -> str:
    provider_type = str(provider_type or "").strip()
    return f"{provider_type}#{index}" if provider_type else ""


def _domain_matches(provider_domain: str, email_domain: str) -> bool:
    provider_domain = str(provider_domain or "").strip().lower()
    email_domain = str(email_domain or "").strip().lower()
    if not provider_domain or not email_domain:
        return False
    if provider_domain.startswith("*."):
        base = provider_domain[2:]
        return email_domain == base or email_domain.endswith(f".{base}")
    return provider_domain == email_domain


def _infer_mail_provider(email: str, mail_config: object) -> tuple[str, str]:
    _, _, email_domain = str(email or "").strip().lower().partition("@")
    if not email_domain or not isinstance(mail_config, dict):
        return "", ""
    providers = mail_config.get("providers")
    if not isinstance(providers, list):
        return "", ""
    fallback: tuple[str, str] = ("", "")
    for index, provider in enumerate(providers, start=1):
        if not isinstance(provider, dict):
            continue
        provider_type = str(provider.get("type") or "").strip()
        if not provider_type:
            continue
        domains = provider.get("domain")
        domains = domains if isinstance(domains, list) else [domains]
        if any(_domain_matches(str(domain or ""), email_domain) for domain in domains):
            candidate = (provider_type, _mail_provider_ref(provider_type, index))
            if provider.get("enable"):
                return candidate
            fallback = fallback or candidate
    return fallback


def _mail_provider_entry_by_ref(mail_config: object, provider_ref: str) -> dict:
    ref = str(provider_ref or "").strip()
    if not ref or not isinstance(mail_config, dict):
        return {}
    providers = mail_config.get("providers")
    if not isinstance(providers, list):
        return {}
    for index, provider in enumerate(providers, start=1):
        if not isinstance(provider, dict):
            continue
        provider_type = str(provider.get("type") or "").strip()
        if _mail_provider_ref(provider_type, index) == ref:
            return dict(provider)
    return {}


def _mailbox_from_registered_record(record: dict, mail_config: object) -> dict:
    email = str(record.get("email") or "").strip()
    mailbox = record.get("mailbox") if isinstance(record.get("mailbox"), dict) else {}
    mailbox = dict(mailbox) if mailbox else {}
    if email and not str(mailbox.get("address") or "").strip():
        mailbox["address"] = email

    provider = str(mailbox.get("provider") or record.get("mail_provider") or "").strip()
    provider_ref = str(mailbox.get("provider_ref") or record.get("mail_provider_ref") or "").strip()
    if not provider or not provider_ref:
        inferred_provider, inferred_provider_ref = _infer_mail_provider(email, mail_config)
        provider = provider or inferred_provider
        provider_ref = provider_ref or inferred_provider_ref

    if provider:
        mailbox["provider"] = provider
    if provider_ref:
        mailbox["provider_ref"] = provider_ref

    entry = _mail_provider_entry_by_ref(mail_config, provider_ref)
    if isinstance(entry, dict) and entry:
        if not str(mailbox.get("api_base") or "").strip() and entry.get("api_base"):
            mailbox["api_base"] = str(entry.get("api_base") or "").rstrip("/")
        domains = entry.get("domain")
        domains = domains if isinstance(domains, list) else [domains]
        _, _, email_domain = email.lower().partition("@")
        matched_domain = next(
            (
                str(domain or "").strip()
                for domain in domains
                if _domain_matches(str(domain or ""), email_domain)
            ),
            "",
        )
        if matched_domain and not str(mailbox.get("domain") or "").strip():
            mailbox["domain"] = matched_domain
        if provider == "moemail" and not str(mailbox.get("email_id") or "").strip() and email:
            mailbox["email_id"] = email
    return mailbox


def _enrich_registered_mail_metadata(record: dict, mail_config: object) -> dict:
    if not isinstance(record, dict):
        return record
    enriched = dict(record)
    mailbox = _mailbox_from_registered_record(enriched, mail_config)
    if mailbox:
        enriched["mailbox"] = mailbox
    if mailbox.get("provider") and not str(enriched.get("mail_provider") or "").strip():
        enriched["mail_provider"] = str(mailbox.get("provider") or "").strip() or None
    if mailbox.get("provider_ref") and not str(enriched.get("mail_provider_ref") or "").strip():
        enriched["mail_provider_ref"] = str(mailbox.get("provider_ref") or "").strip() or None
    return enriched


def _normalize(raw: dict) -> dict:
    cfg = _default_config()
    cfg.update({k: v for k, v in raw.items() if k not in {"stats", "logs"}})
    cfg["total"] = max(1, int(cfg.get("total") or 1))
    cfg["threads"] = max(1, int(cfg.get("threads") or 1))
    cfg["mode"] = str(cfg.get("mode") or "total").strip() if str(cfg.get("mode") or "total").strip() in {"total", "quota", "available"} else "total"
    cfg["target_quota"] = max(1, int(cfg.get("target_quota") or 1))
    cfg["target_available"] = max(1, int(cfg.get("target_available") or 1))
    cfg["check_interval"] = max(1, int(cfg.get("check_interval") or 5))
    cfg["add_to_local_pool"] = bool(cfg.get("add_to_local_pool"))
    cfg["proxy"] = str(cfg.get("proxy") or "").strip()
    cfg["enabled"] = bool(cfg.get("enabled"))
    stats = {**_default_config()["stats"], **(raw.get("stats") if isinstance(raw.get("stats"), dict) else {}),
             "threads": cfg["threads"]}
    cfg["stats"] = stats
    cfg["deleted_registered_emails"] = _normalize_deleted_registered_emails(raw.get("deleted_registered_emails"))
    deleted_emails = set(cfg["deleted_registered_emails"])
    cfg["registered_accounts"] = [
        item
        for item in _normalize_registered_accounts(raw.get("registered_accounts"), cfg.get("mail"))
        if str(item.get("email") or "").strip().lower() not in deleted_emails
    ]
    return cfg


def _normalize_deleted_registered_emails(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    emails: list[str] = []
    seen: set[str] = set()
    for item in raw:
        email = str(item or "").strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        emails.append(email)
    return emails[-2000:]


def _normalize_registered_accounts(raw: object, mail_config: object | None = None) -> list[dict]:
    if not isinstance(raw, list):
        return []
    records: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email") or "").strip()
        access_token = str(item.get("access_token") or "").strip()
        key = email or access_token
        if not key or key in seen:
            continue
        proxy = item.get("proxy") if isinstance(item.get("proxy"), dict) else {}
        oauth = item.get("oauth") if isinstance(item.get("oauth"), dict) else {}
        mailbox = item.get("mailbox") if isinstance(item.get("mailbox"), dict) else {}
        normalized_oauth = dict(oauth) if oauth else {}
        refresh_token = str(item.get("refresh_token") or normalized_oauth.get("refresh_token") or "").strip()
        id_token = str(item.get("id_token") or normalized_oauth.get("id_token") or "").strip()
        if refresh_token:
            normalized_oauth["refresh_token"] = refresh_token
        if id_token:
            normalized_oauth["id_token"] = id_token
        if (access_token or refresh_token or id_token) and not str(normalized_oauth.get("client_id") or "").strip():
            normalized_oauth["client_id"] = openai_register.platform_oauth_client_id
        inferred_provider, inferred_provider_ref = _infer_mail_provider(email, mail_config)
        mail_provider = str(item.get("mail_provider") or item.get("provider") or inferred_provider).strip()
        mail_provider_ref = str(item.get("mail_provider_ref") or item.get("provider_ref") or inferred_provider_ref).strip()
        records.append({
            "email": email,
            "password": str(item.get("password") or "").strip(),
            "mail_provider": mail_provider or None,
            "mail_provider_ref": mail_provider_ref or None,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
            "oauth": normalized_oauth or None,
            "mailbox": dict(mailbox) if mailbox else None,
            "created_at": str(item.get("created_at") or "").strip(),
            "job_id": str(item.get("job_id") or "").strip() or None,
            "proxy_key": str(item.get("proxy_key") or proxy.get("proxy_key") or "").strip() or None,
            "proxy": dict(proxy) if proxy else None,
            "recovered": bool(item.get("recovered")),
            "auth_failed": bool(item.get("auth_failed")),
            "error": str(item.get("error") or "").strip(),
        })
        records[-1] = _enrich_registered_mail_metadata(records[-1], mail_config)
        seen.add(key)
    return records


class RegisterService:
    def __init__(self, store_file: Path):
        self._store_file = store_file
        self._lock = threading.RLock()
        self._runner: threading.Thread | None = None
        self._logs: list[dict] = []
        self._manual_recovery_sessions: dict[str, dict] = {}
        self._register_provider_locks: dict[str, threading.Semaphore] = defaultdict(
            lambda: threading.Semaphore(REGISTER_PROVIDER_CONCURRENCY)
        )
        self._register_domain_locks: dict[str, threading.Semaphore] = defaultdict(
            lambda: threading.Semaphore(REGISTER_DOMAIN_CONCURRENCY)
        )
        openai_register.register_log_sink = self._append_log
        self._config = self._load()
        self._backfill_registered_accounts_from_pool()
        if self._config["enabled"]:
            self.start()

    def _job_log_file(self, job_id: str = "") -> Path:
        target = str(job_id or ((self._config.get("stats") or {}).get("job_id")) or "manual").strip() or "manual"
        return REGISTER_LOG_DIR / f"{target}.jsonl"

    def _load(self) -> dict:
        try:
            return _normalize(json.loads(self._store_file.read_text(encoding="utf-8")))
        except Exception:
            return _normalize({})

    def _save(self) -> None:
        self._store_file.parent.mkdir(parents=True, exist_ok=True)
        self._store_file.write_text(json.dumps(self._config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _cleanup_manual_recovery_sessions_locked(self) -> None:
        now = time.monotonic()
        expired = [
            session_id
            for session_id, session in self._manual_recovery_sessions.items()
            if float(session.get("expires_at") or 0) <= now
        ]
        for session_id in expired:
            session = self._manual_recovery_sessions.pop(session_id, None)
            registrar = session.get("registrar") if isinstance(session, dict) else None
            try:
                if registrar:
                    registrar.close()
            except Exception:
                pass

    def get(self) -> dict:
        with self._lock:
            snapshot = {**self._config, "logs": self._logs[-300:]}
        return json.loads(json.dumps(self._with_registered_pool_statuses(snapshot), ensure_ascii=False))

    def _inject_proxy_to_mail(self) -> None:
        proxy = str(self._config.get("proxy") or "").strip()
        if proxy and isinstance(self._config.get("mail"), dict):
            self._config["mail"]["proxy"] = proxy

    def update(self, updates: dict) -> dict:
        with self._lock:
            self._config = _normalize({**self._config, **updates})
            self._inject_proxy_to_mail()
            openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads")})
            self._save()
            return self.get()

    def start(self) -> dict:
        with self._lock:
            if self._runner and self._runner.is_alive():
                self._config["enabled"] = True
                self._save()
                return self.get()
            self._config["enabled"] = True
            self._inject_proxy_to_mail()
            self._logs = []
            metrics = self._pool_metrics()
            self._config["stats"] = {"job_id": uuid.uuid4().hex, "success": 0, "fail": 0, "done": 0, "running": 0, "threads": self._config["threads"], **metrics, "started_at": _now(), "updated_at": _now()}
            openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads")})
            openai_register.reset_thread_proxy_pool()
            with openai_register.stats_lock:
                openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": time.time()})
            self._save()
            self._runner = threading.Thread(target=self._run, daemon=True, name="openai-register")
            self._runner.start()
            local_pool = "开启" if bool(self._config.get("add_to_local_pool")) else "关闭"
            self._append_log(
                f"注册任务启动，模式={self._config['mode']}，线程数={self._config['threads']}，自动加入本地号池={local_pool}；完整注册信息和日志始终保留",
                "yellow",
            )
            return self.get()

    def stop(self) -> dict:
        with self._lock:
            self._config["enabled"] = False
            self._config["stats"]["updated_at"] = _now()
            self._save()
            self._append_log("已请求停止注册任务，正在等待当前运行任务结束", "yellow")
            return self.get()

    def reset(self) -> dict:
        with self._lock:
            self._logs = []
            self._config["stats"] = {"success": 0, "fail": 0, "done": 0, "running": 0, "threads": self._config["threads"], "elapsed_seconds": 0, "avg_seconds": 0, "success_rate": 0, **self._pool_metrics(), "updated_at": _now()}
            with openai_register.stats_lock:
                openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": 0.0})
            self._save()
            return self.get()

    def registered_accounts(self, job_id: str | None = None) -> list[dict]:
        target = str(job_id or "").strip()
        with self._lock:
            records = list(self._config.get("registered_accounts") or [])
        if target:
            records = [item for item in records if str(item.get("job_id") or "") == target]
        return json.loads(json.dumps(records, ensure_ascii=False))

    def delete_registered_accounts(self, emails: list[str]) -> dict:
        targets = {
            str(email or "").strip().lower()
            for email in emails
            if str(email or "").strip()
        }
        with self._lock:
            if not targets:
                return {"register": self.get(), "removed": 0}
            records = list(self._config.get("registered_accounts") or [])
            kept: list[dict] = []
            removed = 0
            for item in records:
                email = str(item.get("email") or "").strip().lower() if isinstance(item, dict) else ""
                if email in targets:
                    removed += 1
                    continue
                kept.append(item)
            deleted = _normalize_deleted_registered_emails([
                *(self._config.get("deleted_registered_emails") or []),
                *targets,
            ])
            self._config["registered_accounts"] = kept
            self._config["deleted_registered_emails"] = deleted
            self._save()
            return {"register": self.get(), "removed": removed}

    def update_registered_account(self, email: str, updates: dict) -> dict | None:
        target = str(email or "").strip().lower()
        if not target or not isinstance(updates, dict):
            return None
        with self._lock:
            records = list(self._config.get("registered_accounts") or [])
            changed = False
            updated: dict | None = None
            for index, item in enumerate(records):
                if not isinstance(item, dict) or str(item.get("email") or "").strip().lower() != target:
                    continue
                next_item = dict(item)
                if isinstance(updates.get("oauth"), dict) or isinstance(item.get("oauth"), dict):
                    next_item["oauth"] = {
                        **(item.get("oauth") if isinstance(item.get("oauth"), dict) else {}),
                        **(updates.get("oauth") if isinstance(updates.get("oauth"), dict) else {}),
                    }
                for key, value in updates.items():
                    if key == "oauth":
                        continue
                    next_item[key] = value
                records[index] = next_item
                updated = next_item
                changed = True
                break
            if changed:
                self._config["registered_accounts"] = _normalize_registered_accounts(records, self._config.get("mail"))
                self._save()
            return json.loads(json.dumps(updated, ensure_ascii=False)) if updated else None

    def backfill_registered_mail_metadata(self) -> dict:
        with self._lock:
            records = list(self._config.get("registered_accounts") or [])
            next_records: list[dict] = []
            changed = 0
            for item in records:
                if not isinstance(item, dict):
                    next_records.append(item)
                    continue
                before = json.dumps(item, ensure_ascii=False, sort_keys=True)
                enriched = _enrich_registered_mail_metadata(item, self._config.get("mail"))
                after = json.dumps(enriched, ensure_ascii=False, sort_keys=True)
                if before != after:
                    changed += 1
                next_records.append(enriched)
            self._config["registered_accounts"] = _normalize_registered_accounts(next_records, self._config.get("mail"))
            self._save()
            return {"register": self.get(), "updated": changed}

    @staticmethod
    def _registered_account_needs_recovery(record: dict) -> bool:
        if not isinstance(record, dict):
            return False
        oauth = record.get("oauth") if isinstance(record.get("oauth"), dict) else {}
        return bool(
            record.get("auth_failed")
            or not str(record.get("access_token") or "").strip()
            or not str(record.get("refresh_token") or oauth.get("refresh_token") or "").strip()
            or not str(record.get("id_token") or oauth.get("id_token") or "").strip()
        )

    def recover_registered_accounts(self, emails: list[str], *, force: bool = False) -> dict:
        targets = {
            str(email or "").strip().lower()
            for email in emails
            if str(email or "").strip()
        }
        if not targets:
            return {"register": self.get(), "recovered": 0, "errors": []}
        records = {
            str(item.get("email") or "").strip().lower(): item
            for item in self.registered_accounts()
            if isinstance(item, dict) and str(item.get("email") or "").strip().lower() in targets
        }
        recovered = 0
        errors: list[dict] = []
        for target in targets:
            record = records.get(target)
            if not record:
                errors.append({"email": target, "error": "注册记录不存在"})
                continue
            if not force and not self._registered_account_needs_recovery(record):
                errors.append({"email": target, "error": "当前记录未检测到需要重新登录恢复的问题；缺少 chatgpt_account_id 时请先执行导出前检查补齐元数据"})
                continue
            result, error = self._recover_registered_account_record(record)
            if error:
                error_item = {"email": target, "error": error}
                if isinstance(error, str):
                    try:
                        parsed_error = json.loads(error)
                    except Exception:
                        parsed_error = {}
                    if isinstance(parsed_error, dict) and parsed_error.get("manual_code_required"):
                        error_item.update({key: value for key, value in parsed_error.items() if key != "register"})
                        error_item["error"] = str(parsed_error.get("error") or "需要输入邮箱验证码").strip()
                errors.append(error_item)
                continue
            if result:
                recovered += 1
        return {"register": self.get(), "recovered": recovered, "errors": errors}

    def _recover_registered_account_record(self, record: dict) -> tuple[dict | None, str]:
        email = str(record.get("email") or "").strip()
        password = str(record.get("password") or "").strip()
        if not email or not password:
            return None, "缺少邮箱或密码"
        was_imported_to_local_pool = bool(self._registered_account_pool_status(record).get("imported_to_local_pool"))
        mailbox = record.get("mailbox") if isinstance(record.get("mailbox"), dict) else {}
        mailbox = _mailbox_from_registered_record(
            {
                **record,
                "mailbox": mailbox,
                "mail_provider": str(record.get("mail_provider") or "").strip(),
                "mail_provider_ref": str(record.get("mail_provider_ref") or "").strip(),
                "email": email,
            },
            self._config.get("mail"),
        )
        proxy = record.get("proxy") if isinstance(record.get("proxy"), dict) else None
        if not proxy:
            proxy = proxy_by_key(str(record.get("proxy_key") or ""))
        proxy_candidates: list[dict | None] = [proxy]
        seen_proxy_keys = {str((proxy or {}).get("proxy_key") or "").strip()} if proxy else set()
        for candidate in load_available_proxies():
            key = str(candidate.get("proxy_key") or "").strip()
            if key and key in seen_proxy_keys:
                continue
            proxy_candidates.append(candidate)
            if key:
                seen_proxy_keys.add(key)
            if len(proxy_candidates) >= 4:
                break
        if not proxy_candidates:
            proxy_candidates = [None]
        if proxy is None and self._config.get("proxy", "") and None not in proxy_candidates:
            proxy_candidates.append(None)

        last_error = ""
        try:
            self._append_log(f"{email} 开始重新登录恢复凭据", "yellow")
            result = None
            recovered_proxy = proxy
            for attempt, candidate_proxy in enumerate(proxy_candidates, start=1):
                proxy_label = str((candidate_proxy or {}).get("name") or (candidate_proxy or {}).get("proxy_key") or "全局代理/直连")
                self._append_log(f"{email} 尝试恢复登录 {attempt}/{len(proxy_candidates)}: {proxy_label}", "yellow")
                registrar = openai_register.PlatformRegistrar(proxy_url(candidate_proxy) if candidate_proxy else self._config.get("proxy", ""))
                try:
                    result = registrar.recover_existing_account(email, password, mailbox, 0)
                    recovered_proxy = candidate_proxy
                    break
                except Exception as exc:
                    last_error = str(exc) or exc.__class__.__name__
                    self._append_log(f"{email} 当前代理恢复失败: {last_error}", "yellow")
                    if not openai_register.PlatformRegistrar._is_retryable_login_error(last_error):
                        break
                    time.sleep(min(6, 2 * attempt))
                finally:
                    registrar.close()
            if result is None:
                if self._should_offer_manual_code_recovery(last_error):
                    manual_state = self.begin_manual_registered_account_recovery(email)
                    if manual_state.get("status") == "manual_code_required" and manual_state.get("session_id"):
                        return None, json.dumps({
                            "manual_code_required": True,
                            "email": email,
                            **manual_state,
                        }, ensure_ascii=False)
                    raise RuntimeError(str(manual_state.get("error") or last_error or "重新登录恢复失败"))
                raise RuntimeError(last_error or "重新登录恢复失败")
            if recovered_proxy:
                result["proxy"] = recovered_proxy
            result = self._enrich_result_oauth_metadata(result)
            oauth = result.get("oauth") if isinstance(result.get("oauth"), dict) else {}
            updates = {
                "password": password,
                "access_token": str(result.get("access_token") or "").strip(),
                "refresh_token": str(result.get("refresh_token") or "").strip(),
                "id_token": str(result.get("id_token") or "").strip(),
                "oauth": oauth,
                "mailbox": result.get("mailbox") if isinstance(result.get("mailbox"), dict) else mailbox,
                "recovered": True,
                "auth_failed": False,
                "error": "",
                "created_at": str(result.get("created_at") or _now()).strip(),
            }
            if recovered_proxy:
                updates["proxy"] = recovered_proxy
                updates["proxy_key"] = str(recovered_proxy.get("proxy_key") or "").strip() or None
            updated = self.update_registered_account(email, updates)
            if was_imported_to_local_pool and updated:
                if self._add_result_to_local_pool(updated, str(updated.get("job_id") or "")):
                    self._append_log(f"{email} 已同步更新 chat 本地号池凭据", "green")
            self._append_log(f"{email} 重新登录恢复成功", "green")
            return updated, ""
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            if self._is_terminal_account_error(error):
                error = f"{error}；账号已被远端删除或停用，无法通过验证码或重新登录恢复"
            if "缺少 email_id" in error or "缺少 token" in error or "缺少 mailbox" in error:
                error = f"{error}；旧记录缺少邮箱服务元数据，无法自动读取登录验证码，需要手动登录"
            self.update_registered_account(email, {"auth_failed": True, "error": error})
            self._append_log(f"{email} 重新登录恢复失败: {error}", "red")
            return None, error

    @staticmethod
    def _should_offer_manual_code_recovery(error: str) -> bool:
        text = str(error or "")
        if RegisterService._is_terminal_account_error(text):
            return False
        return bool(
            "缺少 email_id" in text
            or "旧记录缺少邮箱服务元数据" in text
            or "独立登录等待验证码超时" in text
        )

    @staticmethod
    def _is_terminal_account_error(error: str) -> bool:
        text = str(error or "").lower()
        return "deleted or deactivated" in text or "account because it has been deleted" in text

    @classmethod
    def is_terminal_account_error(cls, error: str) -> bool:
        return cls._is_terminal_account_error(error)

    def _registered_record_by_email(self, email: str) -> dict | None:
        target = str(email or "").strip().lower()
        if not target:
            return None
        with self._lock:
            for item in self._config.get("registered_accounts") or []:
                if isinstance(item, dict) and str(item.get("email") or "").strip().lower() == target:
                    return json.loads(json.dumps(item, ensure_ascii=False))
        return None

    def _apply_recovered_registered_result(
        self,
        record: dict,
        result: dict,
        recovered_proxy: dict | None,
        *,
        manual: bool = False,
    ) -> dict | None:
        email = str(record.get("email") or result.get("email") or "").strip()
        password = str(record.get("password") or result.get("password") or "").strip()
        was_imported_to_local_pool = bool(self._registered_account_pool_status(record).get("imported_to_local_pool"))
        mailbox = record.get("mailbox") if isinstance(record.get("mailbox"), dict) else {}
        if recovered_proxy:
            result["proxy"] = recovered_proxy
        result = self._enrich_result_oauth_metadata(result)
        oauth = result.get("oauth") if isinstance(result.get("oauth"), dict) else {}
        updates = {
            "password": password,
            "access_token": str(result.get("access_token") or "").strip(),
            "refresh_token": str(result.get("refresh_token") or "").strip(),
            "id_token": str(result.get("id_token") or "").strip(),
            "oauth": oauth,
            "mailbox": result.get("mailbox") if isinstance(result.get("mailbox"), dict) else mailbox,
            "recovered": True,
            "auth_failed": False,
            "error": "",
            "created_at": str(result.get("created_at") or _now()).strip(),
        }
        if recovered_proxy:
            updates["proxy"] = recovered_proxy
            updates["proxy_key"] = str(recovered_proxy.get("proxy_key") or "").strip() or None
        updated = self.update_registered_account(email, updates)
        if was_imported_to_local_pool and updated:
            if self._add_result_to_local_pool(updated, str(updated.get("job_id") or "")):
                self._append_log(f"{email} 已同步更新 chat 本地号池凭据", "green")
        suffix = "（手动验证码）" if manual else ""
        self._append_log(f"{email} 重新登录恢复成功{suffix}", "green")
        return updated

    def begin_manual_registered_account_recovery(self, email: str) -> dict:
        record = self._registered_record_by_email(email)
        if not record:
            return {"status": "error", "error": "注册记录不存在"}
        target_email = str(record.get("email") or "").strip()
        password = str(record.get("password") or "").strip()
        if not target_email or not password:
            return {"status": "error", "error": "缺少邮箱或密码"}
        mailbox = _mailbox_from_registered_record(record, self._config.get("mail"))
        api_base = str(mailbox.get("api_base") or "").strip()
        proxy = record.get("proxy") if isinstance(record.get("proxy"), dict) else None
        if not proxy:
            proxy = proxy_by_key(str(record.get("proxy_key") or ""))
        registrar = openai_register.PlatformRegistrar(proxy_url(proxy) if proxy else self._config.get("proxy", ""))
        try:
            self._append_log(f"{target_email} 开始手动验证码恢复凭据", "yellow")
            state = registrar.begin_manual_existing_account_recovery(target_email, password, 0)
            if state.get("status") == "complete":
                result = {
                    "email": target_email,
                    "password": password,
                    "access_token": str((state.get("tokens") or {}).get("access_token") or "").strip(),
                    "refresh_token": str((state.get("tokens") or {}).get("refresh_token") or "").strip(),
                    "id_token": str((state.get("tokens") or {}).get("id_token") or "").strip(),
                    "mailbox": mailbox,
                    "created_at": _now(),
                    "recovered": True,
                }
                updated = self._apply_recovered_registered_result(record, result, proxy, manual=True)
                registrar.close()
                return {"status": "complete", "register": self.get(), "recovered": 1, "email": target_email, "api_base": api_base}
            if state.get("status") != "manual_code_required":
                registrar.close()
                return {"status": "error", "error": "远端未进入邮箱验证码流程"}
            session_id = uuid.uuid4().hex
            with self._lock:
                self._cleanup_manual_recovery_sessions_locked()
                self._manual_recovery_sessions[session_id] = {
                    "registrar": registrar,
                    "record": record,
                    "proxy": proxy,
                    "code_verifier": str(state.get("code_verifier") or ""),
                    "continue_url": str(state.get("continue_url") or ""),
                    "expires_at": time.monotonic() + 300,
                }
            return {
                "status": "manual_code_required",
                "session_id": session_id,
                "email": target_email,
                "api_base": api_base,
                "mailbox": {
                    key: value
                    for key, value in mailbox.items()
                    if key in {"provider", "provider_ref", "address", "email_id", "domain", "api_base"}
                },
                "expires_in": 300,
            }
        except Exception as exc:
            registrar.close()
            error = str(exc) or exc.__class__.__name__
            if self._is_terminal_account_error(error):
                error = f"{error}；账号已被远端删除或停用，无法通过验证码或重新登录恢复"
            self.update_registered_account(target_email, {"auth_failed": True, "error": error})
            self._append_log(f"{target_email} 手动验证码恢复启动失败: {error}", "red")
            return {"status": "error", "error": error, "register": self.get()}

    def complete_manual_registered_account_recovery(self, session_id: str, code: str) -> dict:
        session_key = str(session_id or "").strip()
        clean_code = str(code or "").strip()
        if not session_key:
            return {"status": "error", "error": "缺少恢复会话"}
        if not clean_code:
            return {"status": "error", "error": "缺少验证码"}
        with self._lock:
            self._cleanup_manual_recovery_sessions_locked()
            session = self._manual_recovery_sessions.pop(session_key, None)
        if not session:
            return {"status": "error", "error": "恢复会话已过期，请重新触发手动验证码恢复"}
        registrar = session.get("registrar")
        record = session.get("record") if isinstance(session.get("record"), dict) else {}
        proxy = session.get("proxy") if isinstance(session.get("proxy"), dict) else None
        email = str(record.get("email") or "").strip()
        password = str(record.get("password") or "").strip()
        try:
            tokens = registrar.complete_manual_existing_account_recovery(
                str(session.get("code_verifier") or ""),
                str(session.get("continue_url") or ""),
                clean_code,
                0,
            )
            result = {
                "email": email,
                "password": password,
                "access_token": str(tokens.get("access_token") or "").strip(),
                "refresh_token": str(tokens.get("refresh_token") or "").strip(),
                "id_token": str(tokens.get("id_token") or "").strip(),
                "mailbox": _mailbox_from_registered_record(record, self._config.get("mail")),
                "created_at": _now(),
                "recovered": True,
            }
            self._apply_recovered_registered_result(record, result, proxy, manual=True)
            return {"status": "complete", "register": self.get(), "recovered": 1, "email": email}
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            if self._is_terminal_account_error(error):
                error = f"{error}；账号已被远端删除或停用，无法通过验证码或重新登录恢复"
            if email:
                self.update_registered_account(email, {"auth_failed": True, "error": error})
                self._append_log(f"{email} 手动验证码恢复失败: {error}", "red")
            return {"status": "error", "error": error, "register": self.get()}
        finally:
            try:
                registrar.close()
            except Exception:
                pass

    def _record_registered_account(self, result: dict, job_id: str) -> None:
        if not isinstance(result, dict):
            return
        record = {
            "email": str(result.get("email") or "").strip(),
            "password": str(result.get("password") or "").strip(),
            "mail_provider": str(result.get("mail_provider") or "").strip() or None,
            "mail_provider_ref": str(result.get("mail_provider_ref") or "").strip() or None,
            "access_token": str(result.get("access_token") or "").strip(),
            "refresh_token": str(result.get("refresh_token") or "").strip(),
            "id_token": str(result.get("id_token") or "").strip(),
            "created_at": str(result.get("created_at") or _now()).strip(),
            "job_id": str(job_id or "").strip() or None,
            "recovered": bool(result.get("recovered")),
            "auth_failed": bool(result.get("auth_failed")),
            "error": str(result.get("error") or "").strip(),
        }
        mailbox = result.get("mailbox") if isinstance(result.get("mailbox"), dict) else {}
        if mailbox:
            record["mailbox"] = dict(mailbox)
        oauth = dict(result.get("oauth")) if isinstance(result.get("oauth"), dict) else {}
        for key in ("refresh_token", "id_token"):
            value = str(record.get(key) or oauth.get(key) or "").strip()
            if value:
                record[key] = value
                oauth[key] = value
        if record["access_token"] and not str(oauth.get("client_id") or "").strip():
            oauth["client_id"] = openai_register.platform_oauth_client_id
        if oauth:
            record["oauth"] = oauth
        if not record["mail_provider"]:
            inferred_provider, inferred_provider_ref = _infer_mail_provider(record["email"], self._config.get("mail"))
            record["mail_provider"] = inferred_provider or None
            record["mail_provider_ref"] = inferred_provider_ref or None
        proxy = result.get("proxy") if isinstance(result.get("proxy"), dict) else {}
        if proxy:
            record["proxy"] = dict(proxy)
            record["proxy_key"] = str(proxy.get("proxy_key") or "").strip() or None
        if not record["email"]:
            return
        with self._lock:
            records = [item for item in self._config.get("registered_accounts") or [] if str(item.get("email") or "").strip() != record["email"]]
            records.append(record)
            self._config["registered_accounts"] = records[-1000:]
            self._config["deleted_registered_emails"] = [
                email
                for email in _normalize_deleted_registered_emails(self._config.get("deleted_registered_emails"))
                if email != record["email"].lower()
            ]
            self._save()

    @staticmethod
    def _created_at_from_account(account: dict) -> str:
        oauth = account.get("oauth") if isinstance(account.get("oauth"), dict) else {}
        token_version = oauth.get("_token_version")
        try:
            if token_version:
                return datetime.fromtimestamp(int(token_version) / 1000, tz=timezone.utc).isoformat()
        except Exception:
            pass
        access_payload = account_service._decode_jwt_payload(str(account.get("access_token") or ""))
        try:
            issued_at = int(access_payload.get("iat") or 0)
            if issued_at > 0:
                return datetime.fromtimestamp(issued_at, tz=timezone.utc).isoformat()
        except Exception:
            pass
        return str(account.get("created_at") or account.get("registered_at") or _now()).strip()

    def _registered_record_from_pool_account(self, account: dict) -> dict | None:
        if not isinstance(account, dict):
            return None
        login = account.get("login") if isinstance(account.get("login"), dict) else {}
        oauth = account.get("oauth") if isinstance(account.get("oauth"), dict) else {}
        email = str(account.get("email") or login.get("email") or oauth.get("email") or "").strip()
        password = str(login.get("password") or "").strip()
        if not email or not password:
            return None
        record = {
            "email": email,
            "password": password,
            "mail_provider": str(account.get("mail_provider") or login.get("mail_provider") or "").strip() or None,
            "mail_provider_ref": str(account.get("mail_provider_ref") or login.get("mail_provider_ref") or "").strip() or None,
            "access_token": str(account.get("access_token") or "").strip(),
            "refresh_token": str(oauth.get("refresh_token") or "").strip(),
            "id_token": str(oauth.get("id_token") or "").strip(),
            "oauth": {
                **oauth,
                "client_id": str(oauth.get("client_id") or openai_register.platform_oauth_client_id).strip(),
            },
            "created_at": self._created_at_from_account(account),
            "job_id": str(account.get("register_job_id") or "").strip() or None,
            "recovered": bool(account.get("recovered")),
            "auth_failed": False,
            "error": str(account.get("last_error") or "").strip(),
        }
        proxy = account.get("proxy") if isinstance(account.get("proxy"), dict) else {}
        if proxy:
            record["proxy"] = dict(proxy)
            record["proxy_key"] = str(proxy.get("proxy_key") or account.get("proxy_key") or "").strip() or None
        else:
            record["proxy_key"] = str(account.get("proxy_key") or "").strip() or None
        if not record["mail_provider"]:
            inferred_provider, inferred_provider_ref = _infer_mail_provider(record["email"], self._config.get("mail"))
            record["mail_provider"] = inferred_provider or None
            record["mail_provider_ref"] = inferred_provider_ref or None
        return record

    def _backfill_registered_accounts_from_pool(self) -> None:
        records = list(self._config.get("registered_accounts") or [])
        deleted_emails = set(_normalize_deleted_registered_emails(self._config.get("deleted_registered_emails")))
        by_email = {
            str(item.get("email") or "").strip(): item
            for item in records
            if isinstance(item, dict) and str(item.get("email") or "").strip()
        }
        added = False
        for account in account_service.list_accounts():
            record = self._registered_record_from_pool_account(account)
            if not record:
                continue
            if record["email"].lower() in deleted_emails:
                continue
            current = by_email.get(record["email"])
            if current is None:
                records.append(record)
                by_email[record["email"]] = record
                added = True
                continue
            oauth = current.get("oauth") if isinstance(current.get("oauth"), dict) else {}
            next_oauth = record.get("oauth") if isinstance(record.get("oauth"), dict) else {}
            missing_oauth = bool(next_oauth) and any(
                not str(oauth.get(key) or "").strip()
                for key in ("chatgpt_account_id", "client_id", "refresh_token", "id_token")
            )
            if missing_oauth:
                merged_oauth = dict(oauth)
                for key, value in next_oauth.items():
                    if value and not str(merged_oauth.get(key) or "").strip():
                        merged_oauth[key] = value
                current["oauth"] = merged_oauth
                for key in ("refresh_token", "id_token", "access_token", "proxy_key"):
                    if not str(current.get(key) or "").strip() and str(record.get(key) or "").strip():
                        current[key] = record[key]
                if not isinstance(current.get("proxy"), dict) and isinstance(record.get("proxy"), dict):
                    current["proxy"] = record["proxy"]
                added = True
        if added:
            self._config["registered_accounts"] = _normalize_registered_accounts(records, self._config.get("mail"))[-1000:]
            self._save()

    def _add_result_to_local_pool(self, result: dict, job_id: str) -> bool:
        try:
            record = openai_register.build_account_pool_record(result, job_id)
            for key in ("mail_provider", "mail_provider_ref", "mailbox"):
                value = result.get(key)
                if value:
                    record[key] = value
            record.update({
                "status": "正常",
                "quota": 0,
                "image_quota_unknown": True,
                "credential_owner": "chatgpt2api",
            })
            account_service.add_account_records([record])
            email = str(record.get("email") or result.get("email") or "").strip()
            self._append_log(f"{email} 已加入 chat 本地号池（未刷新远端信息）", "yellow")
            return True
        except Exception as exc:
            email = str(result.get("email") or "").strip()
            self._append_log(f"{email} 加入 chat 本地号池失败: {exc}", "red")
            return False

    def sync_registered_account_to_local_pool(self, email: str) -> bool:
        target = str(email or "").strip().lower()
        if not target:
            return False
        record = None
        for item in self.registered_accounts():
            if str(item.get("email") or "").strip().lower() == target:
                record = item
                break
        if not record:
            return False
        if not str(record.get("access_token") or "").strip():
            return False
        return self._add_result_to_local_pool(record, str(record.get("job_id") or ""))

    def _registered_account_pool_status(self, item: dict) -> dict:
        email = str(item.get("email") or "").strip().lower()
        access_token = str(item.get("access_token") or "").strip()
        if not email and not access_token:
            return {"imported_to_local_pool": False, "local_pool_status": ""}
        for account in account_service.list_accounts():
            login = account.get("login") if isinstance(account.get("login"), dict) else {}
            account_email = str(account.get("email") or login.get("email") or "").strip().lower()
            account_token = str(account.get("access_token") or "").strip()
            if (access_token and account_token == access_token) or (email and account_email == email):
                return {
                    "imported_to_local_pool": True,
                    "local_pool_status": str(account.get("status") or "").strip(),
                }
        return {"imported_to_local_pool": False, "local_pool_status": ""}

    def _with_registered_pool_statuses(self, config: dict) -> dict:
        records = []
        for item in config.get("registered_accounts") or []:
            if not isinstance(item, dict):
                continue
            records.append({**item, **self._registered_account_pool_status(item)})
        return {**config, "registered_accounts": records}

    def import_registered_accounts_to_pool(self, emails: list[str]) -> dict:
        targets = {
            str(email or "").strip().lower()
            for email in emails
            if str(email or "").strip()
        }
        with self._lock:
            records = [
                dict(item)
                for item in self._config.get("registered_accounts") or []
                if isinstance(item, dict)
            ]
        selected = [
            item
            for item in records
            if (not targets or str(item.get("email") or "").strip().lower() in targets)
        ]
        imported = 0
        skipped = 0
        errors: list[dict] = []
        for item in selected:
            email = str(item.get("email") or "").strip()
            if not str(item.get("access_token") or "").strip():
                skipped += 1
                errors.append({"email": email, "error": "缺少 access_token，需先重新登录恢复凭据"})
                continue
            if self._add_result_to_local_pool(item, str(item.get("job_id") or "")):
                imported += 1
            else:
                skipped += 1
                errors.append({"email": email, "error": "加入 chat 本地号池失败"})
        return {
            "register": self.get(),
            "imported": imported,
            "skipped": skipped,
            "errors": errors,
        }

    def _enrich_result_oauth_metadata(self, result: dict) -> dict:
        access_token = str(result.get("access_token") or "").strip()
        if not access_token:
            return result
        try:
            from services.openai_backend_api import OpenAIBackendAPI

            remote = OpenAIBackendAPI(access_token).get_user_info()
        except Exception as exc:
            self._append_log(f"{result.get('email') or '新账号'} 注册成功，但补充 chatgpt_account_id 失败: {exc}", "yellow")
            return result
        remote_oauth = remote.get("oauth") if isinstance(remote.get("oauth"), dict) else {}
        if not remote_oauth:
            return result
        oauth = result.get("oauth") if isinstance(result.get("oauth"), dict) else {}
        result["oauth"] = {
            **oauth,
            **remote_oauth,
            "client_id": str(oauth.get("client_id") or openai_register.platform_oauth_client_id).strip(),
        }
        if remote.get("email") and not result.get("email"):
            result["email"] = remote.get("email")
        return result

    def _append_log(self, text: str, color: str = "") -> None:
        item = {"time": _now(), "text": str(text), "level": str(color or "info")}
        with self._lock:
            self._logs.append(item)
            self._logs = self._logs[-300:]
            job_id = str((self._config.get("stats") or {}).get("job_id") or "").strip()
        if job_id:
            try:
                REGISTER_LOG_DIR.mkdir(parents=True, exist_ok=True)
                with self._job_log_file(job_id).open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            except Exception:
                pass

    def _pool_metrics(self) -> dict:
        items = account_service.list_accounts()
        normal = [item for item in items if item.get("status") == "正常"]
        return {
            "current_quota": sum(int(item.get("quota") or 0) for item in normal if not item.get("image_quota_unknown")),
            "current_available": len(normal),
        }

    def _target_reached(self, cfg: dict, submitted: int) -> bool:
        mode = str(cfg.get("mode") or "total")
        metrics = self._pool_metrics()
        self._bump(**metrics)
        if mode == "quota":
            reached = metrics["current_quota"] >= int(cfg.get("target_quota") or 1)
            self._append_log(f"检查号池：当前正常账号={metrics['current_available']}，当前剩余额度={metrics['current_quota']}，目标额度={cfg.get('target_quota')}，{'跳过注册' if reached else '继续注册'}", "yellow")
            return reached
        if mode == "available":
            reached = metrics["current_available"] >= int(cfg.get("target_available") or 1)
            self._append_log(f"检查号池：当前正常账号={metrics['current_available']}，目标账号={cfg.get('target_available')}，当前剩余额度={metrics['current_quota']}，{'跳过注册' if reached else '继续注册'}", "yellow")
            return reached
        return submitted >= int(cfg.get("total") or 1)

    def _bump(self, **updates) -> None:
        with self._lock:
            self._config["stats"].update(updates)
            stats = self._config["stats"]
            started_at = str(stats.get("started_at") or "")
            if started_at:
                try:
                    elapsed = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds())
                except Exception:
                    elapsed = 0.0
                done = int(stats.get("done") or 0)
                success = int(stats.get("success") or 0)
                fail = int(stats.get("fail") or 0)
                stats["elapsed_seconds"] = round(elapsed, 1)
                stats["avg_seconds"] = round(elapsed / success, 1) if success else 0
                stats["success_rate"] = round(success * 100 / max(1, success + fail), 1)
            self._config["stats"]["updated_at"] = _now()
            self._save()

    def _mail_concurrency_keys(self, task_index: int) -> tuple[str, str]:
        providers = ((self.get().get("mail") or {}).get("providers") or [])
        enabled: list[dict] = [item for item in providers if isinstance(item, dict) and item.get("enable")]
        if not enabled:
            return "mail:none", "domain:none"
        provider = enabled[(max(1, task_index) - 1) % len(enabled)]
        provider_type = str(provider.get("type") or "unknown").strip() or "unknown"
        api_base = str(provider.get("api_base") or provider.get("cf_api_base") or provider.get("base_url") or "").strip().rstrip("/")
        provider_key = f"{provider_type}:{api_base or task_index}"
        domains = provider.get("domain") or provider.get("default_domain") or []
        if isinstance(domains, str):
            domains = [item.strip() for item in domains.splitlines() if item.strip()]
        if not isinstance(domains, list):
            domains = []
        domain = str(domains[(max(1, task_index) - 1) % len(domains)]).strip().lower() if domains else ""
        return provider_key, f"{provider_key}:{domain or 'domain:none'}"

    def _register_worker(self, index: int, job_id: str) -> dict:
        provider_key, domain_key = self._mail_concurrency_keys(index)
        with self._lock:
            provider_lock = self._register_provider_locks[provider_key]
            domain_lock = self._register_domain_locks[domain_key]
        with ExitStack() as stack:
            stack.enter_context(provider_lock)
            stack.enter_context(domain_lock)
            return openai_register.worker(index, job_id)

    def _run(self) -> None:
        threads = int(self.get()["threads"])
        submitted, done, success, fail = 0, 0, 0, 0
        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = set()
            while True:
                cfg = self.get()
                cooldown_wait = openai_register.register_proxy_cooldown_wait_seconds()
                if cooldown_wait > 0 and not futures:
                    self._append_log(f"所有注册代理都在冷却中，等待 {int(cooldown_wait)} 秒后继续", "yellow")
                    time.sleep(min(30.0, cooldown_wait))
                    continue
                while self.get()["enabled"] and not self._target_reached(cfg, submitted) and len(futures) < threads:
                    cooldown_wait = openai_register.register_proxy_cooldown_wait_seconds()
                    if cooldown_wait > 0:
                        if not futures:
                            self._append_log(f"所有注册代理都在冷却中，等待 {int(cooldown_wait)} 秒后继续", "yellow")
                            time.sleep(min(30.0, cooldown_wait))
                        break
                    submitted += 1
                    futures.add(executor.submit(self._register_worker, submitted, str(cfg.get("stats", {}).get("job_id") or "")))
                self._bump(running=len(futures), done=done, success=success, fail=fail)
                if not futures and (not self.get()["enabled"] or str(cfg.get("mode") or "total") == "total"):
                    break
                if not futures:
                    time.sleep(max(1, int(cfg.get("check_interval") or 5)))
                    continue
                finished, futures = wait(futures, return_when=FIRST_COMPLETED)
                for future in finished:
                    done += 1
                    try:
                        result = future.result()
                        if result.get("ok"):
                            success += 1
                            payload = result.get("result") if isinstance(result.get("result"), dict) else {}
                            job_id = str(cfg.get("stats", {}).get("job_id") or "")
                            payload = self._enrich_result_oauth_metadata(payload)
                            self._record_registered_account(payload, job_id)
                            if bool(cfg.get("add_to_local_pool")):
                                self._add_result_to_local_pool(payload, job_id)
                        else:
                            fail += 1
                            payload = result.get("registered") if isinstance(result.get("registered"), dict) else {}
                            self._record_registered_account(payload, str(cfg.get("stats", {}).get("job_id") or ""))
                    except Exception:
                        fail += 1
        self._bump(running=0, done=done, success=success, fail=fail, finished_at=_now())
        with self._lock:
            self._config["enabled"] = False
            self._save()
        self._append_log(f"注册任务结束，成功{success}，失败{fail}", "yellow")


register_service = RegisterService(REGISTER_FILE)
