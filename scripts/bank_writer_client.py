"""Private, typed channel access to the shared bank writer; never a model tool."""
import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request


class BankWriteError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise BankWriteError("写入服务不能重定向")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class BankWriterClient:
    def __init__(self, url, accounts):
        target = urllib.parse.urlsplit(url)
        if (target.scheme != "http" or target.hostname != "127.0.0.1" or not target.port or target.username or target.password
                or target.path not in ("", "/") or target.query or target.fragment):
            raise BankWriteError("写入服务必须使用明确的本机私有地址")
        self.url = url.rstrip("/") + "/private/bank/v1"
        if not isinstance(accounts, dict) or not accounts:
            raise BankWriteError("需要显式配置维护者的服务凭据")
        self.accounts = accounts
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def request(self, sender, operation, payload):
        if sender not in self.accounts or operation not in {"reserve", "prepare", "status", "withdraw", "review", "confirm"}:
            raise BankWriteError("此身份没有该题库操作权限")
        owner = operation in ("review", "confirm")
        try:
            file = Path(self.accounts[sender]["owner_file" if owner else "prepare_file"])
            if file.stat().st_size > 8192:
                raise ValueError()
            credentials = json.loads(file.read_bytes())
            if (set(credentials) != {"token", "subject"} or credentials["subject"] != sender
                    or not isinstance(credentials["token"], str) or not 32 <= len(credentials["token"]) <= 1024):
                raise ValueError()
        except Exception:
            raise BankWriteError("题库服务凭据不可用") from None
        body = json.dumps({"operation": operation, "payload": payload}, ensure_ascii=False, allow_nan=False).encode()
        if len(body) > 64 * 1024 * 1024:
            raise BankWriteError("本批图片过大，请减少图片后重试")
        request = urllib.request.Request(self.url, data=body, method="POST", headers={"Content-Type": "application/json",
            "Authorization": "Bearer " + credentials["token"], "X-Bank-Subject": sender})
        try:
            try:
                response = self.opener.open(request, timeout=45)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise ValueError()
                result = json.loads(raw)
                ok = response.status == 200 and result.get("ok") is True
        except Exception:
            raise BankWriteError("写入服务暂不可用；请查询原操作状态，勿重新新增") from None
        if not ok:
            messages = {"bank-version-changed": "题库已变化，请撤销旧计划后重新准备",
                "published-operation-cannot-be-cancelled": "这题已入库，请用原题号准备修改或删除",
                "approval-does-not-match-review": "确认与当前计划不一致，请重新查看",
                "reservation-not-owned": "预留题号不属于当前任务", "operation-not-owned": "入库操作不属于当前任务"}
            raise BankWriteError(messages.get(result.get("error"), "原操作尚未完成，请查询状态后继续"))
        return result["result"]
