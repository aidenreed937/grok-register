import asyncio
import json
import re
import zipfile
from datetime import datetime
from io import BytesIO

import orjson
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from app.core.auth import get_app_key, verify_app_key
from app.core.batch import create_task, expire_task, get_task
from app.core.logger import logger
from app.core.storage import get_storage
from app.services.grok.batch_services.usage import UsageService
from app.services.grok.batch_services.nsfw import NSFWService
from app.services.token.cpa_export import CpaExportError, sso_to_cpa_entry
from app.services.token.manager import get_token_manager

router = APIRouter()
_CPA_EXPORTS: dict[str, dict] = {}

_TOKEN_CHAR_REPLACEMENTS = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u00a0": " ",
        "\u2007": " ",
        "\u202f": " ",
        "\u200b": "",
        "\u200c": "",
        "\u200d": "",
        "\ufeff": "",
    }
)


def _sanitize_token_text(value) -> str:
    token = "" if value is None else str(value)
    token = token.translate(_TOKEN_CHAR_REPLACEMENTS)
    token = re.sub(r"\s+", "", token)
    if token.startswith("sso="):
        token = token[4:]
    return token.encode("ascii", errors="ignore").decode("ascii")


def _mask_token(token: str) -> str:
    return f"{token[:8]}...{token[-8:]}" if len(token) > 20 else token


def _extract_sanitized_tokens(data: dict) -> list[str]:
    tokens = []
    if isinstance(data.get("token"), str) and data["token"].strip():
        tokens.append(data["token"].strip())
    if isinstance(data.get("tokens"), list):
        tokens.extend([str(t).strip() for t in data["tokens"] if str(t).strip()])
    unique_tokens = list(dict.fromkeys(_sanitize_token_text(t) for t in tokens if t))
    return [t for t in unique_tokens if t]


def _parse_cpa_retries(data: dict) -> int:
    try:
        max_retries = int(data.get("retries") or 8)
    except (TypeError, ValueError):
        max_retries = 8
    return min(max(1, max_retries), 20)


def _build_cpa_export_zip(
    *,
    total: int,
    successes: list[dict],
    failures: list[dict],
    entries: list[tuple[str, dict]],
) -> bytes:
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for filename, entry in entries:
            zf.writestr(
                filename,
                json.dumps(entry, separators=(",", ":"), ensure_ascii=False),
            )
        zf.writestr(
            "export-summary.json",
            json.dumps(
                {
                    "total": total,
                    "success": len(successes),
                    "failed": len(failures),
                    "files": successes,
                    "failures": failures,
                },
                indent=2,
                ensure_ascii=False,
            ),
        )
    return zip_buffer.getvalue()


@router.get("/tokens", dependencies=[Depends(verify_app_key)])
async def get_tokens():
    """获取所有 Token"""
    # 获取消耗模式配置
    from app.core.config import get_config
    mgr = await get_token_manager()
    results = {}
    for pool_name, pool in mgr.pools.items():
        results[pool_name] = [t.model_dump() for t in pool.list()]
    consumed_mode = get_config("token.consumed_mode_enabled", False)
    return {
        "tokens": results or {},
        "consumed_mode_enabled": consumed_mode,
    }


@router.post("/tokens", dependencies=[Depends(verify_app_key)])
async def update_tokens(data: dict):
    """更新 Token 信息"""
    storage = get_storage()
    try:
        from app.services.token.models import TokenInfo

        async with storage.acquire_lock("tokens_save", timeout=10):
            existing = await storage.load_tokens() or {}
            normalized = {}
            allowed_fields = set(TokenInfo.model_fields.keys())
            existing_map = {}
            for pool_name, tokens in existing.items():
                if not isinstance(tokens, list):
                    continue
                pool_map = {}
                for item in tokens:
                    if isinstance(item, str):
                        token_data = {"token": item}
                    elif isinstance(item, dict):
                        token_data = dict(item)
                    else:
                        continue
                    raw_token = token_data.get("token")
                    if raw_token is not None:
                        token_data["token"] = _sanitize_token_text(raw_token)
                    token_key = token_data.get("token")
                    if isinstance(token_key, str):
                        pool_map[token_key] = token_data
                existing_map[pool_name] = pool_map
            for pool_name, tokens in (data or {}).items():
                if not isinstance(tokens, list):
                    continue
                pool_list = []
                for item in tokens:
                    if isinstance(item, str):
                        token_data = {"token": item}
                    elif isinstance(item, dict):
                        token_data = dict(item)
                    else:
                        continue

                    raw_token = token_data.get("token")
                    if raw_token is not None:
                        token_data["token"] = _sanitize_token_text(raw_token)
                    if not token_data.get("token"):
                        logger.warning(f"Skip empty token in pool '{pool_name}'")
                        continue

                    base = existing_map.get(pool_name, {}).get(
                        token_data.get("token"), {}
                    )
                    merged = dict(base)
                    merged.update(token_data)
                    if merged.get("tags") is None:
                        merged["tags"] = []

                    filtered = {k: v for k, v in merged.items() if k in allowed_fields}
                    try:
                        info = TokenInfo(**filtered)
                        pool_list.append(info.model_dump())
                    except Exception as e:
                        logger.warning(f"Skip invalid token in pool '{pool_name}': {e}")
                        continue
                normalized[pool_name] = pool_list

            await storage.save_tokens(normalized)
            mgr = await get_token_manager()
            await mgr.reload()
        return {"status": "success", "message": "Token 已更新"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tokens/refresh", dependencies=[Depends(verify_app_key)])
async def refresh_tokens(data: dict):
    """刷新 Token 状态"""
    try:
        mgr = await get_token_manager()
        tokens = []
        if isinstance(data.get("token"), str) and data["token"].strip():
            tokens.append(data["token"].strip())
        if isinstance(data.get("tokens"), list):
            tokens.extend([str(t).strip() for t in data["tokens"] if str(t).strip()])

        if not tokens:
            raise HTTPException(status_code=400, detail="No tokens provided")

        unique_tokens = list(dict.fromkeys(tokens))

        raw_results = await UsageService.batch(
            unique_tokens,
            mgr,
        )

        # 强制保存变更到存储
        await mgr._save(force=True)

        results = {}
        for token, res in raw_results.items():
            results[token] = bool(res.get("ok")) and res.get("data") is True

        response = {"status": "success", "results": results}
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tokens/export/cpa", dependencies=[Depends(verify_app_key)])
async def export_cpa_tokens(data: dict):
    """将选中的 SSO token 导出为 cli-proxy-api xAI OAuth 文件 zip。"""
    unique_tokens = _extract_sanitized_tokens(data)
    if not unique_tokens:
        raise HTTPException(status_code=400, detail="No tokens provided")

    max_retries = _parse_cpa_retries(data)

    zip_buffer = BytesIO()
    successes = []
    failures = []
    used_names = set()

    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for index, token in enumerate(unique_tokens, 1):
            try:
                filename, entry = await asyncio.to_thread(
                    sso_to_cpa_entry,
                    token,
                    "",
                    max_retries,
                )
                if filename in used_names:
                    stem, suffix = filename.rsplit(".", 1)
                    filename = f"{stem}-{index}.{suffix}"
                used_names.add(filename)
                zf.writestr(
                    filename,
                    json.dumps(entry, separators=(",", ":"), ensure_ascii=False),
                )
                successes.append({"token": token[:8] + "..." + token[-8:], "file": filename})
            except CpaExportError as exc:
                failures.append({"token": token[:8] + "..." + token[-8:], "error": str(exc)})
            except Exception as exc:
                failures.append({"token": token[:8] + "..." + token[-8:], "error": str(exc)})

        zf.writestr(
            "export-summary.json",
            json.dumps(
                {
                    "total": len(unique_tokens),
                    "success": len(successes),
                    "failed": len(failures),
                    "files": successes,
                    "failures": failures,
                },
                indent=2,
                ensure_ascii=False,
            ),
        )

    if not successes:
        detail = failures[0]["error"] if failures else "CPA export failed"
        raise HTTPException(status_code=502, detail=detail)

    filename = f"cpa_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.zip"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(zip_buffer.getvalue(), media_type="application/zip", headers=headers)


@router.post("/tokens/export/cpa/async", dependencies=[Depends(verify_app_key)])
async def export_cpa_tokens_async(data: dict):
    """将选中的 SSO token 异步导出为 cli-proxy-api xAI OAuth 文件 zip。"""
    unique_tokens = _extract_sanitized_tokens(data)
    if not unique_tokens:
        raise HTTPException(status_code=400, detail="No tokens provided")

    max_retries = _parse_cpa_retries(data)
    task = create_task(len(unique_tokens))

    async def _run():
        successes = []
        failures = []
        entries = []
        used_names = set()
        try:
            for index, token in enumerate(unique_tokens, 1):
                if task.cancelled:
                    task.finish_cancelled()
                    return

                masked = _mask_token(token)
                try:
                    filename, entry = await asyncio.to_thread(
                        sso_to_cpa_entry,
                        token,
                        "",
                        max_retries,
                    )
                    if filename in used_names:
                        stem, suffix = filename.rsplit(".", 1)
                        filename = f"{stem}-{index}.{suffix}"
                    used_names.add(filename)
                    entries.append((filename, entry))
                    successes.append({"token": masked, "file": filename})
                    task.record(True, item=masked, detail={"file": filename})
                except CpaExportError as exc:
                    error = str(exc)
                    failures.append({"token": masked, "error": error})
                    task.record(False, item=masked, error=error)
                except Exception as exc:
                    error = str(exc)
                    failures.append({"token": masked, "error": error})
                    task.record(False, item=masked, error=error)

            result = {
                "status": "success" if successes else "failed",
                "summary": {
                    "total": len(unique_tokens),
                    "ok": len(successes),
                    "fail": len(failures),
                },
                "files": successes,
                "failures": failures,
            }

            if successes:
                filename = f"cpa_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.zip"
                _CPA_EXPORTS[task.id] = {
                    "filename": filename,
                    "content": _build_cpa_export_zip(
                        total=len(unique_tokens),
                        successes=successes,
                        failures=failures,
                        entries=entries,
                    ),
                }
                result["download_url"] = f"/v1/admin/tokens/export/cpa/{task.id}/download"

            warning = None
            if failures:
                warning = failures[0]["error"] if not successes else f"{len(failures)} failed"
            task.finish(result, warning=warning)
        except Exception as e:
            task.fail_task(str(e))
        finally:
            async def _cleanup():
                await expire_task(task.id, 600)
                _CPA_EXPORTS.pop(task.id, None)

            asyncio.create_task(_cleanup())

    asyncio.create_task(_run())

    return {
        "status": "success",
        "task_id": task.id,
        "total": len(unique_tokens),
    }


@router.get("/tokens/export/cpa/{task_id}/download", dependencies=[Depends(verify_app_key)])
async def download_cpa_export(task_id: str):
    export = _CPA_EXPORTS.get(task_id)
    if not export:
        raise HTTPException(status_code=404, detail="CPA export not found or expired")

    headers = {"Content-Disposition": f'attachment; filename="{export["filename"]}"'}
    return Response(export["content"], media_type="application/zip", headers=headers)


@router.post("/tokens/refresh/async", dependencies=[Depends(verify_app_key)])
async def refresh_tokens_async(data: dict):
    """刷新 Token 状态（异步批量 + SSE 进度）"""
    mgr = await get_token_manager()
    tokens = []
    if isinstance(data.get("token"), str) and data["token"].strip():
        tokens.append(data["token"].strip())
    if isinstance(data.get("tokens"), list):
        tokens.extend([str(t).strip() for t in data["tokens"] if str(t).strip()])

    if not tokens:
        raise HTTPException(status_code=400, detail="No tokens provided")

    unique_tokens = list(dict.fromkeys(tokens))

    task = create_task(len(unique_tokens))

    async def _run():
        try:

            async def _on_item(item: str, res: dict):
                task.record(bool(res.get("ok")) and res.get("data") is True)

            raw_results = await UsageService.batch(
                unique_tokens,
                mgr,
                on_item=_on_item,
                should_cancel=lambda: task.cancelled,
            )

            if task.cancelled:
                task.finish_cancelled()
                return

            results: dict[str, bool] = {}
            ok_count = 0
            fail_count = 0
            for token, res in raw_results.items():
                if res.get("ok") and res.get("data") is True:
                    ok_count += 1
                    results[token] = True
                else:
                    fail_count += 1
                    results[token] = False

            await mgr._save(force=True)

            result = {
                "status": "success",
                "summary": {
                    "total": len(unique_tokens),
                    "ok": ok_count,
                    "fail": fail_count,
                },
                "results": results,
            }
            task.finish(result)
        except Exception as e:
            task.fail_task(str(e))
        finally:
            import asyncio
            asyncio.create_task(expire_task(task.id, 300))

    import asyncio
    asyncio.create_task(_run())

    return {
        "status": "success",
        "task_id": task.id,
        "total": len(unique_tokens),
    }


@router.get("/batch/{task_id}/stream")
async def batch_stream(task_id: str, request: Request):
    app_key = get_app_key()
    if app_key:
        key = request.query_params.get("app_key")
        if key != app_key:
            raise HTTPException(status_code=401, detail="Invalid authentication token")
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    async def event_stream():
        queue = task.attach()
        try:
            yield f"data: {orjson.dumps({'type': 'snapshot', **task.snapshot()}).decode()}\n\n"

            final = task.final_event()
            if final:
                yield f"data: {orjson.dumps(final).decode()}\n\n"
                return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    final = task.final_event()
                    if final:
                        yield f"data: {orjson.dumps(final).decode()}\n\n"
                        return
                    continue

                yield f"data: {orjson.dumps(event).decode()}\n\n"
                if event.get("type") in ("done", "error", "cancelled"):
                    return
        finally:
            task.detach(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/batch/{task_id}/cancel", dependencies=[Depends(verify_app_key)])
async def batch_cancel(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task.cancel()
    return {"status": "success"}


@router.post("/tokens/nsfw/enable", dependencies=[Depends(verify_app_key)])
async def enable_nsfw(data: dict):
    """批量开启 NSFW (Unhinged) 模式"""
    try:
        mgr = await get_token_manager()

        tokens = []
        if isinstance(data.get("token"), str) and data["token"].strip():
            tokens.append(data["token"].strip())
        if isinstance(data.get("tokens"), list):
            tokens.extend([str(t).strip() for t in data["tokens"] if str(t).strip()])

        if not tokens:
            for pool_name, pool in mgr.pools.items():
                for info in pool.list():
                    raw = (
                        info.token[4:] if info.token.startswith("sso=") else info.token
                    )
                    tokens.append(raw)

        if not tokens:
            raise HTTPException(status_code=400, detail="No tokens available")

        unique_tokens = list(dict.fromkeys(tokens))

        raw_results = await NSFWService.batch(
            unique_tokens,
            mgr,
        )

        results = {}
        ok_count = 0
        fail_count = 0

        for token, res in raw_results.items():
            masked = f"{token[:8]}...{token[-8:]}" if len(token) > 20 else token
            if res.get("ok") and res.get("data", {}).get("success"):
                ok_count += 1
                results[masked] = res.get("data", {})
            else:
                fail_count += 1
                results[masked] = res.get("data") or {"error": res.get("error")}

        response = {
            "status": "success",
            "summary": {
                "total": len(unique_tokens),
                "ok": ok_count,
                "fail": fail_count,
            },
            "results": results,
        }

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Enable NSFW failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tokens/nsfw/enable/async", dependencies=[Depends(verify_app_key)])
async def enable_nsfw_async(data: dict):
    """批量开启 NSFW (Unhinged) 模式（异步批量 + SSE 进度）"""
    mgr = await get_token_manager()

    tokens = []
    if isinstance(data.get("token"), str) and data["token"].strip():
        tokens.append(data["token"].strip())
    if isinstance(data.get("tokens"), list):
        tokens.extend([str(t).strip() for t in data["tokens"] if str(t).strip()])

    if not tokens:
        for pool_name, pool in mgr.pools.items():
            for info in pool.list():
                raw = info.token[4:] if info.token.startswith("sso=") else info.token
                tokens.append(raw)

    if not tokens:
        raise HTTPException(status_code=400, detail="No tokens available")

    unique_tokens = list(dict.fromkeys(tokens))

    task = create_task(len(unique_tokens))

    async def _run():
        try:

            async def _on_item(item: str, res: dict):
                ok = bool(res.get("ok") and res.get("data", {}).get("success"))
                task.record(ok)

            raw_results = await NSFWService.batch(
                unique_tokens,
                mgr,
                on_item=_on_item,
                should_cancel=lambda: task.cancelled,
            )

            if task.cancelled:
                task.finish_cancelled()
                return

            results = {}
            ok_count = 0
            fail_count = 0
            for token, res in raw_results.items():
                masked = f"{token[:8]}...{token[-8:]}" if len(token) > 20 else token
                if res.get("ok") and res.get("data", {}).get("success"):
                    ok_count += 1
                    results[masked] = res.get("data", {})
                else:
                    fail_count += 1
                    results[masked] = res.get("data") or {"error": res.get("error")}

            await mgr._save(force=True)

            result = {
                "status": "success",
                "summary": {
                    "total": len(unique_tokens),
                    "ok": ok_count,
                    "fail": fail_count,
                },
                "results": results,
            }
            task.finish(result)
        except Exception as e:
            task.fail_task(str(e))
        finally:
            import asyncio
            asyncio.create_task(expire_task(task.id, 300))

    import asyncio
    asyncio.create_task(_run())

    return {
        "status": "success",
        "task_id": task.id,
        "total": len(unique_tokens),
    }
