"""Feishu intake/replace/delete through the same owner-approved writer as Lida."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from PIL import Image
from scripts.bank_writer_client import BankWriteError, BankWriterClient, digest
from scripts.feishu_business_store import FeishuBusinessStore, event_digest
from scripts.feishu_event_security import VerifiedCallback
from scripts.feishu_store_flow import CHAPTERS, is_store_entry_command
from scripts.feishu_delete_flow import parse_delete_choice
from tiku_shared.bank_versions import read_version
from tiku_shared.bank_publication import reject_links, sync_directory


class FeishuBankManagement:
    def __init__(self, config, *, app_id, maintainers, client, classify, fallback, candidate, autostart=True):
        if set(config) != {"state_dir", "published_store", "writer_url", "accounts"}:
            raise ValueError("飞书题库管理配置不完整")
        self.published = reject_links(Path(config["published_store"]))
        directory = reject_links(Path(config["state_dir"]))
        source = Path(__file__).resolve().parents[1]
        for root in (self.published, source):
            if directory == root or root in directory.parents or directory in root.parents:
                raise ValueError("飞书业务状态必须独立于源码和正式题库")
        if set(config["accounts"]) != set(maintainers) or not maintainers:
            raise ValueError("维护者清单必须与渠道服务凭据一一对应")
        self.app_id, self.maintainers, self.client = app_id, tuple(maintainers), client
        self.classify, self.fallback, self.candidate = classify, fallback, candidate
        self.writer = BankWriterClient(config["writer_url"], config["accounts"])
        self.store = FeishuBusinessStore(directory, app_id)
        self.media = self.store.root / "media"; self.media.mkdir(exist_ok=True)
        self.incoming = self.store.root / "incoming"; self.incoming.mkdir(exist_ok=True)
        self.wake, self.stopped = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._run, name="feishu-bank-tasks", daemon=True)
        if autostart:
            self.thread.start()

    @staticmethod
    def context(sender, chat):
        return digest([sender, chat])

    def admit(self, callback):
        if not isinstance(callback, VerifiedCallback) or callback.app_id != self.app_id or callback.sender not in self.maintainers:
            raise ValueError("题库消息需要已核验的维护者身份")
        header, message = callback.payload["header"], callback.payload["event"]["message"]
        content = json.loads(message.get("content", "{}"))
        if not isinstance(content, dict):
            raise ValueError("消息内容无效")
        item = {"id": header["event_id"], "message_id": message["message_id"], "sender": callback.sender,
            "chat_id": message["chat_id"], "created_ms": int(message["create_time"]), "kind": message.get("message_type"),
            "text": content.get("text", ""), "image_key": content.get("image_key", "")}
        if (item["kind"] not in ("text", "image") or not isinstance(item["text"], str) or len(item["text"]) > 20000
                or not isinstance(item["image_key"], str) or len(item["image_key"]) > 256
                or item["kind"] == "image" and not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", item["image_key"])):
            raise ValueError("只支持题图图片和文字操作")
        created = self.store.admit(item)
        self.wake.set()
        return {"ok": True, "accepted" if created else "duplicate": item["message_id"]}

    def close(self):
        self.stopped.set(); self.wake.set()
        if self.thread.is_alive():
            self.thread.join()
        self.store.close()

    def _run(self):
        while not self.stopped.is_set():
            worked = self.process_one()
            worked = self.process_job() or worked
            if not worked:
                self.wake.wait(1); self.wake.clear()

    def process_one(self):
        row = self.store.next()
        if not row:
            return False
        try:
            event = json.loads(row["event"])
            if event_digest(event) != row["body_hash"] or event["sender"] not in self.maintainers:
                raise ValueError("业务消息身份不一致")
            if row["state"] == "pending":
                try:
                    task, response = self.respond(event, bound_task=self.store.by_id(row["task_id"]) if row["task_id"] else None)
                except Exception:
                    # In particular, do not keep charging for failed recognition.
                    # The saved image can be retried by an explicit user message.
                    task = None
                    response = {"items": [{"text": "本次处理未完成，消息和已有草稿已保留。请查询状态后继续；题图识别失败可回复 重试识别。"}]}
                self.store.complete(event["id"], task, response)
            else:
                response = json.loads(row["response"])
            self.deliver(event, response)
        except Exception:
            # Delivery retries are bounded; the original receipt stays queryable.
            self.store.retry(row["id"])
        return True

    def process_job(self):
        row = self.store.next_job()
        if not row:
            return False
        task, trusted = None, False
        try:
            task = self.store.by_id(row["task_id"])
            event = self.store.event(row["event_id"])
            if (not task or task.get("batch", {}).get("key") != row["batch_key"] or task["phase"] == "cancelled"):
                self.store.job_replied(row["id"])
                return True
            if (event["sender"] not in self.maintainers or event["sender"] != task["sender"]
                    or self.context(event["sender"], event["chat_id"]) != task["context"]):
                raise ValueError("业务任务归属不一致")
            trusted = True
            if row["state"] == "processed":
                response = json.loads(row["response"])
            else:
                status = self._call(task, "status", client_key=row["batch_key"])
                if row["kind"] == "prepare":
                    if task["phase"] != "preparing":
                        self.store.job_replied(row["id"])
                        return True
                    if status["state"] == "not-submitted" or status["state"] == "failed" and not row["dispatched"]:
                        if digest(self._typed(task)) != task["batch"]["request_hash"]:
                            raise BankWriteError("草稿与固定请求不一致")
                        self.store.dispatched(row["id"])
                        status = self._call(task, "prepare", client_key=row["batch_key"],
                            expected_publication=task["batch"]["publication"], plans=[self._typed(task)])
                    if status["state"] in {"preparing", "approved", "publishing"}:
                        self.store.defer_job(row["id"])
                        return True
                    response = self._preview(task, status) if status["state"] == "prepared" else {"items": [{"text": self._status_text(task, status)}]}
                elif row["kind"] == "confirm":
                    approval = task.get("approval", {})
                    batch = task["batch"]
                    if (event["kind"] != "text" or event["text"].strip() != "1"
                            or approval.get("event_id") != event["id"] or approval.get("message_id") != event["message_id"]
                            or approval.get("created_ms") != event["created_ms"] or not batch.get("delivered_ms")
                            or event["created_ms"] <= batch["delivered_ms"] or approval.get("digest") != batch.get("digest")
                            or approval.get("challenge") != batch.get("challenge")):
                        raise BankWriteError("确认与已展示计划不一致")
                    self._assert_batch(task, status)
                    if status["state"] not in {"published", "cancelled", "conflict"}:
                        status = self._call(task, "confirm", operation_id=status["operation_id"],
                            plan_digest=approval["digest"], challenge=approval["challenge"])
                    if status["state"] in {"prepared", "approved", "publishing"}:
                        self.store.defer_job(row["id"])
                        return True
                    response = {"items": [{"text": self._status_text(task, status)}]}
                else:
                    raise ValueError("业务阶段无效")
                self.store.complete_job(row["id"], task, response)
            self.deliver(event, response, job_id=row["id"])
        except Exception:
            paused = self.store.defer_job(row["id"], failed=True) == "paused"
            if paused and row["state"] == "pending" and task and trusted:
                # This is an inability to establish the outcome, not proof that
                # a confirmed write failed. Keep its intent and operation key.
                self.store.complete_job(row["id"], task, {"items": [{"text":
                    f"题号：{task['question_id']}\n暂时无法核实本次操作结果，自动重试已暂停。草稿和原操作已保留；回复 状态 查询后继续。"}]})
        return True

    def _save_image(self, data):
        if not data or len(data) > 20 * 1024 * 1024:
            raise BankWriteError("图片过大或无法读取")
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 40_000_000 or image.format not in {"JPEG", "PNG", "WEBP", "GIF", "BMP"}:
                raise BankWriteError("图片格式或尺寸不支持")
            image.verify()
        key = hashlib.sha256(data).hexdigest() + ".jpg"
        path = reject_links(self.media / key)
        if not path.exists():
            temporary = self.media / ("." + uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(data); stream.flush(); os.fsync(stream.fileno())
                os.replace(temporary, path)
                sync_directory(self.media)
            finally:
                temporary.unlink(missing_ok=True)
        if path.read_bytes() != data:
            raise BankWriteError("已保存的图片内容不一致")
        return key

    def _image_bytes(self, key):
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}\.jpg", key):
            raise BankWriteError("任务图片身份无效")
        path = reject_links(self.media / key)
        if path.stat().st_size > 20 * 1024 * 1024:
            raise BankWriteError("任务图片过大")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != key[:-4]:
            raise BankWriteError("任务图片已变化")
        return data

    def _published(self):
        file = self.published / "active.json"
        if file.stat().st_size > 8192:
            raise BankWriteError("题库发布状态无效")
        pointer = json.loads(file.read_bytes())
        if pointer.get("schema") != 1 or type(pointer.get("revision")) is not int or pointer["revision"] < 1:
            raise BankWriteError("题库发布状态无效")
        version = read_version(self.published, pointer.get("version"))
        directory = version.main.parent
        manifest = json.loads((directory / "manifest.json").read_bytes())
        entry = next(item for item in manifest["files"] if item["path"] == "registry.json")
        file = reject_links(directory / "registry.json")
        if file.stat().st_size > 32 * 1024 * 1024:
            raise BankWriteError("题号表无效")
        raw = file.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise BankWriteError("题号表与发布版本不一致")
        return pointer, version, json.loads(raw)

    def _call(self, task, operation, **payload):
        return self.writer.request(task["sender"], operation, {"scope": task["scope"], **payload})

    def _new(self, event, change="add-record"):
        context = self.context(event["sender"], event["chat_id"])
        identity = "fs_" + digest([self.app_id, context, event["message_id"]])[:32]
        return {"id": identity, "context": context, "sender": event["sender"], "scope": "feishu/" + digest([context, identity]),
            "change": change, "phase": "question", "revision": 1, "answers": [], "last_event_ms": event["created_ms"]}

    def _withdraw(self, task):
        if task.get("batch"):
            status = self._call(task, "withdraw", client_key=task["batch"]["key"])
            if status["state"] != "cancelled":
                raise BankWriteError("旧计划尚未撤销")
            task.pop("batch"); task.pop("approval", None)
            task["revision"] += 1
            task["phase"] = "answers"
            self.store.save(task)

    def _typed(self, task):
        plan = {"id": task["id"], "question_id": task["question_id"], "change": task["change"]}
        if task["change"] == "add-record":
            plan["reservation_key"] = task["id"]
        else:
            plan["expected_record_revision"] = task["before"]["revision"]
        if task["change"] != "delete-record":
            plan["fields"] = task["fields"]
            plan["images"] = [{"data": base64.b64encode(self._image_bytes(key)).decode(), "sha256": key[:-4]}
                              for key in [task["question"], *task["answers"]]]
        return plan

    def _status_text(self, task, status):
        if status["state"] == "published":
            task["phase"] = "published"
            task["receipt"] = {key: status["result"][key] for key in ("operation_id", "version", "revision")}
            verb = {"add-record": "已存入", "revise-record": "已修改", "delete-record": "已删除"}[task["change"]]
            return f"{verb}题目：{task['question_id']}\n章节：{task['fields']['chapter']}\n答案：{len(task['answers']) if task['change'] != 'delete-record' else 0} 张\n入库记录：{status['operation_id']}\n发布序号：{status['result']['revision']}"
        if status["state"] in {"failed", "conflict", "cancelled"}:
            task["phase"] = status["state"]
        messages = {"prepared": "计划已准备，等待确认", "preparing": "正在核对题目和答案，请稍等", "approved": "已确认，等待写入", "publishing": "正在存入，请稍等",
            "failed": "准备未完成，可回复 1 重试；修改或取消会撤销旧计划", "conflict": "题库已变化，请取消旧计划后重新准备", "cancelled": "旧计划已取消", "not-submitted": "原操作尚未提交，可回复 1 继续"}
        return f"题号：{task['question_id']}\n{messages.get(status['state'], '原操作状态待核实')}"

    def _preview(self, task, status):
        self._assert_batch(task, status)
        review = self._call(task, "review", operation_id=status["operation_id"])
        if review["plan_digest"] != status["plan_digest"]:
            raise BankWriteError("固定计划已变化")
        task["phase"] = "review"
        task["batch"].update({"digest": review["plan_digest"], "challenge": review["approval_challenge"], "delivered_ms": None})
        change = review["plan"]["summary"]["changes"][0]
        if change["question_id"] != task["question_id"] or change["change"] != task["change"]:
            raise BankWriteError("固定计划题号不一致")
        record = change["before"] if task["change"] == "delete-record" else change["after"]
        fields = record["fields"]
        loads = "，".join(f"{load['type']} {load.get('original_raw', load['raw'])}" for load in fields["loads"])
        title = {"add-record": "新增", "revise-record": "替换答案", "delete-record": "删除"}[task["change"]]
        bank = {"main": "主库", "symbolic": "字母库"}[record["bank"]]
        text = f"准备{title}：{task['question_id']}\n章节：{record['chapter']}\n题库：{bank}\n结构类型：{fields.get('structure_type') or '未指定'}\n尺寸：{fields.get('long_width') or '无'}\n单边尺寸：{fields.get('single_side') or '无'}\n荷载：{loads}"
        image_keys = [task["question"], *task["answers"]]
        if task["change"] == "revise-record":
            before = task["before_images"]
            items = [{"text": text + "\n原题和原答案："}, *[{"image": key} for key in before], {"text": "替换后题目和答案："}]
        else:
            items = [{"text": text + ("\n将删除以下题目与答案关联；共享图片保留。" if task["change"] == "delete-record" else "\n题目及答案按以下顺序存入：")}]
        items += [{"image": key} for key in image_keys]
        items.append({"text": "回复 1 确认本计划并写入题库，回复 0 取消。"})
        return {"items": items, "review": {"task_id": task["id"], "key": task["batch"]["key"], "digest": review["plan_digest"]}}

    def _assert_batch(self, task, status):
        typed = self._typed(task)
        if digest(typed) != task["batch"]["request_hash"]:
            raise BankWriteError("草稿或图片已变化，不能确认旧计划")
        reduced = {key: value for key, value in typed.items() if key != "images"}
        if "images" in typed:
            reduced["images"] = [{"sha256": image["sha256"]} for image in typed["images"]]
        plan = status.get("plan", {})
        if plan.get("base") != task["batch"]["publication"] or plan.get("summary", {}).get("request") != [reduced]:
            raise BankWriteError("固定计划与当前任务不一致")

    def _prepare(self, task, event):
        if not task.get("batch"):
            publication = task.get("base") or self._published()[0]
            task["batch"] = {"key": "batch_" + digest([task["id"], task["revision"], publication]), "publication": publication,
                "initiator": event["message_id"], "request_hash": digest(self._typed(task))}
            task["phase"] = "preparing"
        if digest(self._typed(task)) != task["batch"]["request_hash"]:
            raise BankWriteError("草稿与固定请求不一致")
        task["phase"] = "preparing"
        self.store.schedule(task, event["id"], "prepare")
        return {"items": [{"text": f"题号：{task['question_id']}\n正在核对题目和答案，请稍等。完成后会发送预览，确认前不会存入题库。"}],
            "progress": {"task_id": task["id"], "key": task["batch"]["key"], "phase": "preparing"}}

    def _confirm(self, task, event):
        batch = task["batch"]
        if not task.get("approval"):
            if (not batch.get("delivered_ms") or event["created_ms"] <= batch["delivered_ms"]
                    or event["message_id"] == batch["initiator"]):
                return {"items": [{"text": "请先查看本次计划及全部图片，再回复 1 确认。"}]}
            if digest(self._typed(task)) != batch["request_hash"]:
                raise BankWriteError("草稿或图片已变化，不能确认旧计划")
            task["approval"] = {"event_id": event["id"], "message_id": event["message_id"], "created_ms": event["created_ms"],
                "digest": batch["digest"], "challenge": batch["challenge"]}
            task["phase"] = "confirming"
        self.store.schedule(task, task["approval"]["event_id"], "confirm")
        verb = {"add-record": "存入", "revise-record": "修改", "delete-record": "删除"}[task["change"]]
        return {"items": [{"text": f"已确认题目：{task['question_id']}\n正在{verb}，请稍等。完成后会通知结果，无需重复确认。"}],
            "progress": {"task_id": task["id"], "key": batch["key"], "phase": "confirming"}}

    def _recognize(self, task, event, key):
        task["pending_question"] = key
        self.store.save(task, event_id=event["id"])
        try:
            self.client.reply_text(event["message_id"], f"题图已收到，正在识别，请稍等。\n预留题号：{task['question_id']}")
        except Exception:
            pass  # A progress notice failure must not repeat the recognition call.
        draft = self.classify(self.media / key)
        if not draft.loads or draft.route not in {"main", "symbolic"}:
            raise BankWriteError("识别结果需要复核。原题图已保存，可回复 重试识别，或发送新题图。")
        task["question"] = self._save_image(Path(draft.question_image_path).read_bytes())
        task["fields"] = {"chapter": draft.chapter or "", "target_bank": draft.route, "loads": draft.loads,
            "structure_type": draft.structure_type, "long_width": draft.long_width, "single_side": draft.single_side}
        task.pop("pending_question", None)
        task["phase"] = "answers" if draft.chapter else "chapter"
        return {"items": [{"text": f"题图已识别。题号：{task['question_id']}\n章节：{draft.chapter or '待选择'}\n" +
            ("请发送答案图，发完回复 1，0 取消。" if draft.chapter else "请选择章节：\n" + "\n".join(CHAPTERS))}]}

    def _existing(self, event, question_id=None, rank=None, change="delete-record"):
        pointer, version, registry = self._published()
        if rank is not None:
            path = Path(self.candidate(event, rank)["path"])
            # Search candidates must come from this immutable version. A legacy
            # path or an older candidate is never silently rebound to current data.
            if not path.is_absolute() or version.main.resolve() not in path.resolve().parents:
                raise BankWriteError("搜索来源已经变化，请重新检索后再操作")
            matches = [row for row in registry["records"].values() if row["status"] == "active"
                       and (version.main / row["locator"]).resolve() == path.resolve()]
            if len(matches) != 1:
                raise BankWriteError("候选没有唯一的永久题号")
            question_id = matches[0]["id"]
        record = registry["records"].get(question_id)
        if not record or record["status"] != "active":
            raise BankWriteError("题号不存在或已删除")
        task = self._new(event, change)
        task.update({"question_id": question_id, "base": pointer, "before": record,
            "fields": {"chapter": record["chapter"], "target_bank": record["bank"],
                **{name: record["fields"].get(name, "") for name in ("structure_type", "long_width", "single_side")},
                "loads": [{"type": load["type"], "raw": str(load.get("original_raw", load["raw"]))} for load in record["fields"]["loads"]]}})
        images = []
        for media in record["media"]:
            path = reject_links(version.main / media["locator"])
            if version.main.resolve() not in path.parents or path.stat().st_size > 20 * 1024 * 1024:
                raise BankWriteError("原图来源不完整")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != media["sha256"]:
                raise BankWriteError("原图已变化")
            images.append(self._save_image(raw))
        task.update({"before_images": images, "question": images[0], "answers": images[1:] if change == "delete-record" else [], "phase": "answers"})
        return task

    def respond(self, event, bound_task=None):
        context = self.context(event["sender"], event["chat_id"])
        task = bound_task or self.store.task(context)
        if task and (task["context"] != context or task["sender"] != event["sender"]):
            raise ValueError("业务任务归属不一致")
        if task:
            self.store.bind(event["id"], task["id"])
        text = event["text"].strip()
        reply = lambda message: {"items": [{"text": message}]}
        if task and event["created_ms"] < task.get("last_event_ms", 0):
            return task, reply("这条消息早于当前任务，请查看状态后重新发送。")
        try:
            if task and text in {"0", "取消"} and task["phase"] not in {"published", "cancelled"}:
                self._withdraw(task); task["phase"] = "cancelled"
                return task, reply("已取消本次题库操作，已保存资料和题号仍保留。")
            if task and task["phase"] == "confirming" and text not in {"入库记录", "存入记录"}:
                if text in {"1", "状态", "进度"}:
                    return task, self._confirm(task, event)
                return task, reply("上一次确认仍在处理；这条新指令尚未执行。请等存入结果后再发送，查询进度回复 状态。")
            if event["kind"] == "text" and is_store_entry_command(text):
                task = self._new(event)
                self.store.save(task, event_id=event["id"])
                task["question_id"] = self._call(task, "reserve", draft_key=task["id"])["question_id"]
                return task, reply(f"已进入新增题目模式，请发送题目图。\n预留题号：{task['question_id']}\n0 取消")
            if task and task["change"] == "add-record" and not task.get("question_id"):
                task["question_id"] = self._call(task, "reserve", draft_key=task["id"])["question_id"]
            if event["kind"] == "text" and text in {"入库记录", "存入记录"}:
                history = self.store.history(context)
                lines = []
                for item in history:
                    status = self._call(item, "status", client_key=item["batch"]["key"]) if item.get("batch") else None
                    lines.append(f"任务 {item['id']}\n" + (self._status_text(item, status) if status else f"预留题号：{item.get('question_id', '待分配')}，草稿已保存"))
                return task, reply("\n\n".join(lines) or "当前对话没有入库记录。")
            if text.startswith("继续任务 "):
                selected = self.store.by_id(text.removeprefix("继续任务 ").strip())
                if not selected or selected["context"] != context:
                    raise BankWriteError("这个任务不属于当前飞书对话")
                task = selected
                task["search_active"] = False
                text = "状态"
            rank = parse_delete_choice(text) if event["kind"] == "text" else None
            command = re.fullmatch(r"(删除|替换答案)\s+(Q_[a-f0-9-]{36})", text)
            if text in {"删除刚才存入", "删掉刚才存的"}:
                previous = next((item for item in self.store.history(context) if item["change"] == "add-record" and item.get("batch")), None)
                if not previous or self._call(previous, "status", client_key=previous["batch"]["key"])["state"] != "published":
                    raise BankWriteError("最近一次存入尚未确认成功，请先查询入库记录")
                command = re.fullmatch(r"(删除|替换答案)\s+(Q_[a-f0-9-]{36})", "删除 " + previous["question_id"])
            if command or rank is not None:
                change = "revise-record" if command and command[1] == "替换答案" else "delete-record"
                if not bound_task or task["id"] != self._new(event, change)["id"]:
                    task = self._existing(event, command[2] if command else None, rank=rank, change=change)
                self.store.save(task, event_id=event["id"])
                if change == "delete-record":
                    return task, self._prepare(task, event)
                return task, reply(f"正在修改 {task['question_id']} 的答案。\n请发送完整的新答案，发完回复 1；原答案将在确认后被全部替换，0 取消。")
            if not task or task["phase"] in {"published", "cancelled"}:
                if task and (text in {"状态", "进度"} or text == "1" and not task.get("search_active")) and task.get("batch"):
                    return task, reply(self._status_text(task, self._call(task, "status", client_key=task["batch"]["key"])))
                if task:
                    task["search_active"] = True
                return task, self.fallback(event)
            if text in {"状态", "进度"}:
                if task.get("batch"):
                    status = self._call(task, "status", client_key=task["batch"]["key"])
                    return task, self._preview(task, status) if status["state"] == "prepared" else reply(self._status_text(task, status))
                if task.get("pending_question"):
                    return task, reply(f"题号：{task['question_id']}\n原题图已保存，识别尚未完成。回复 重试识别 可使用原图继续。")
                return task, reply(f"预留题号：{task['question_id']}\n草稿已保存，{len(task['answers'])} 张答案。发完答案回复 1。")
            if text == "重试识别" and task["phase"] == "question" and task.get("pending_question"):
                return task, self._recognize(task, event, task["pending_question"])
            if event["kind"] == "image":
                if task["change"] == "delete-record":
                    return task, reply("当前是删除计划，请回复 1 确认或 0 取消。")
                self._withdraw(task)
                path = self.incoming / (digest(event["message_id"]) + ".jpg")
                self.client.download_message_image(event["message_id"], event["image_key"], path)
                if path.stat().st_size > 20 * 1024 * 1024:
                    raise BankWriteError("图片超过单张 20 MiB 限制")
                key = self._save_image(path.read_bytes())
                if task["phase"] == "question":
                    return task, self._recognize(task, event, key)
                if task["phase"] == "chapter":
                    return task, reply("请先选择章节，再发送答案图。")
                if key not in task["answers"]:
                    if len(task["answers"]) >= 100:
                        raise BankWriteError("每道题最多一百张答案")
                    task["answers"].append(key)
                task["phase"] = "answers"
                return task, reply(f"已保存 {len(task['answers'])} 张答案。继续发送，发完回复 1，0 取消。")
            if task["phase"] == "chapter":
                chapter = next((value for value in CHAPTERS if text == value or text == value[0]), None)
                if not chapter:
                    return task, reply("请选择章节：\n" + "\n".join(CHAPTERS))
                task["fields"]["chapter"] = chapter; task["phase"] = "answers"
                return task, reply(f"章节已设为 {chapter}。请发送答案图，发完回复 1。")
            correction = re.fullmatch(r"(?:修改|更正)(尺寸|结构类型|单边尺寸|章节)\s*[:：]?\s*(.+)", text)
            if correction and task.get("fields") and task["change"] != "delete-record":
                self._withdraw(task)
                field = {"尺寸": "long_width", "结构类型": "structure_type", "单边尺寸": "single_side", "章节": "chapter"}[correction[1]]
                if field == "chapter" and correction[2] not in CHAPTERS:
                    raise BankWriteError("章节不在题库范围内")
                task["fields"][field] = correction[2]; task["phase"] = "answers"
                return task, reply("草稿已修改，旧计划已失效。发完答案回复 1 重新核对。")
            if text == "1" and task["phase"] == "review":
                return task, self._confirm(task, event)
            if text == "1" and task.get("question") and task.get("answers"):
                return task, self._prepare(task, event)
            return task, reply("请发送题目或答案图片；答案发完回复 1，查询进度回复 状态，取消回复 0。")
        except BankWriteError as error:
            return task, reply(str(error))
        finally:
            if task:
                task["last_event_ms"] = max(task.get("last_event_ms", 0), event["created_ms"])

    def deliver(self, event, response, *, job_id=None):
        mark = (lambda task=None: self.store.job_replied(job_id, task)) if job_id else (lambda task=None: self.store.replied(event["id"], task))
        progress = response.get("progress")
        if progress:
            current = self.store.by_id(progress["task_id"])
            active = self.store.task(current["context"]) if current else None
            if (not current or not active or active["id"] != current["id"]
                    or current.get("batch", {}).get("key") != progress["key"] or current["phase"] != progress["phase"]):
                mark()
                return
        review = response.get("review")
        task = self.store.by_id(review["task_id"]) if review else None
        active = self.store.task(task["context"]) if task else None
        if review and (not task or not active or active["id"] != task["id"] or task.get("batch", {}).get("key") != review["key"]
                       or task["batch"].get("digest") != review["digest"] or task["phase"] != "review"):
            mark()
            return
        for item in response["items"]:
            if "text" in item:
                self.client.reply_text(event["message_id"], item["text"])
            else:
                self._image_bytes(item["image"])
                self.client.reply_image(event["message_id"], self.media / item["image"])
        if task:
            task["batch"]["delivered_ms"] = int(time.time() * 1000)
        mark(task)
