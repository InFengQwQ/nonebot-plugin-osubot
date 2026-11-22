import os
import subprocess
import tempfile
import shutil
from pathlib import Path
from typing import Optional, Tuple, List
import zipfile
import io
import asyncio
import time

import httpx
import aiohttp
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, Message, MessageSegment
from nonebot.log import logger
from nonebot.typing import T_State
from nonebot_plugin_alconna import UniMessage

from ..utils import NGM
from ..api import osu_api
from .utils import split_msg
from ..file import download_osu
from ..exceptions import NetworkError
from ..mania import generate_preview_pic
from ..draw.osu_preview import draw_osu_preview
from ..draw.catch_preview import draw_cath_preview
from ..draw.taiko_preview import parse_map, map_to_image

# 导入 MC 渲染器（假设 ExportImage.py 已修补，提供 render_mc_to_bytes）
import importlib.util
import sys
from pathlib import Path

from ..ExportImage import render_mc_to_bytes
from src.plugin_runtime import required_malody_account, tool_path, MissingBusinessAccount
from urllib.parse import urlencode

# ========================= Malody API =========================
DOWNLOAD_API = "http://api.paimonnokoya.me/api/store/download"
CONCURRENCY = 4
TIMEOUT = 60

async def download_by_cid(cid: int, cache_dir: Path) -> Optional[Path]:
    """根据 Malody CID 下载谱面，返回本地 .mc 或 .mcz 文件路径"""
    uid, store_key = await required_malody_account()
    ts = int(time.time())
    url = DOWNLOAD_API + "?" + urlencode({"api": "202409", "uid": uid, "key": store_key, "cid": cid, "t": ts})

    connector = aiohttp.TCPConnector(limit=CONCURRENCY * 2)
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with aiohttp.ClientSession(connector=connector) as session:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                data = await resp.json()
        except Exception as e:
            logger.error(f"[Malody CID {cid}] 获取元数据失败: {e}")
            return None

        if data.get("code") != 0 or not data.get("items"):
            logger.error(f"[Malody CID {cid}] 无效元数据")
            return None

        items = data["items"]

        async def download_item(item):
            file_url = item["file"]
            async with semaphore:
                try:
                    async with session.get(file_url, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
                        if r.status == 200:
                            return item["name"], await r.read()
                except Exception as e:
                    logger.warning(f"[Malody] 下载 {item['name']} 失败: {e}")
            return item["name"], None

        tasks = [download_item(item) for item in items]
        results = await asyncio.gather(*tasks)

        valid = [(name, data) for name, data in results if data]
        if not valid:
            return None

        # 如果只有一个 .mc 文件，直接返回该文件
        if len(valid) == 1 and valid[0][0].lower().endswith(".mc"):
            name, data = valid[0]
            out_path = cache_dir / name
            out_path.write_bytes(data)
            return out_path

        # 否则打包成 .mcz
        out_path = cache_dir / f"malody_{cid}.mcz"
        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as mcz:
            for name, data in valid:
                try:
                    # 如果下载的文件本身是 zip，则解压后添加
                    with zipfile.ZipFile(io.BytesIO(data)) as inner:
                        for info in inner.infolist():
                            if info.is_dir():
                                continue
                            mcz.writestr(info.filename, inner.read(info))
                except zipfile.BadZipFile:
                    mcz.writestr(name, data)
        return out_path

CACHE_DIR = Path("cache/malody")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ========================= 原 osu 工具函数 =========================
MA2OM_PATH = tool_path("ma2om.exe")
MAX_HITOBJECTS = 100000

def convert_mcz_to_osu(mc_path: Path) -> Path:
    """将 .mc 文件转换为 .osu（仅用于旧版回退，此处保留但不再主动使用）"""
    work_dir = Path(tempfile.mkdtemp())
    dest_path = None
    try:
        mcz_path = work_dir / (mc_path.stem + ".mcz")
        with zipfile.ZipFile(mcz_path, 'w', zipfile.ZIP_STORED) as zf:
            zf.write(mc_path, mc_path.name)

        result = subprocess.run(
            [str(MA2OM_PATH), str(mcz_path)],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError(f"ma2om.exe 转换失败: {result.stderr}")

        osz_path = mcz_path.with_suffix(".osz")
        if not osz_path.exists():
            raise FileNotFoundError("转换后的 .osz 文件未找到")

        extract_dir = work_dir / "osz_extracted"
        extract_dir.mkdir()
        with zipfile.ZipFile(osz_path, 'r') as zf:
            zf.extractall(extract_dir)

        osu_files = list(extract_dir.glob("*.osu"))
        if not osu_files:
            raise FileNotFoundError("未在 .osz 中找到 .osu 文件")

        final_osu = osu_files[0]
        with tempfile.NamedTemporaryFile(delete=False, suffix=".osu") as f:
            dest_path = Path(f.name)
        shutil.copy2(final_osu, dest_path)
        return dest_path
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

def count_hitobjects(file_path: Path) -> int:
    """统计 .osu 物件数量，超过阈值返回 -1"""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception:
        return -1

    in_section = False
    count = 0
    for line in lines:
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].lower()
            if section in ("hitobjects", "objects"):
                in_section = True
                continue
            else:
                in_section = False
        elif in_section and line != "" and not line.startswith("//"):
            count += 1
            if count > MAX_HITOBJECTS:
                return -1
    return count

def extract_ids_from_osu(file_path: Path) -> Tuple[Optional[int], Optional[int]]:
    """从 .osu 提取 (beatmap_id, beatmapset_id)"""
    bmid = None
    bmsid = None
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("BeatmapID:"):
                    bmid = int(line.split(":")[1].strip())
                elif line.startswith("BeatmapSetID:"):
                    bmsid = int(line.split(":")[1].strip())
                if bmid is not None and bmsid is not None:
                    break
    except Exception:
        pass
    return bmid, bmsid

# ========================= MC 渲染与发送辅助 =========================
def render_mc_preview(mc_path: Path, show_events=False, show_groups=False,
                      max_beats_per_col=80, scale=1.0) -> bytes:
    """调用自定义渲染器生成 MC 预览图字节流"""
    if not mc_path.exists():
        raise FileNotFoundError(f"MC 文件不存在: {mc_path}")
    return render_mc_to_bytes(str(mc_path), show_events=show_events,
                              show_groups=show_groups,
                              max_beats_per_col=max_beats_per_col,
                              scale=scale)

async def send_mc_images(bot: Bot, event: MessageEvent, images: List[bytes], reply=False):
    """发送多张图片，3张及以上使用合并转发"""
    if not images:
        return
    if len(images) < 3:
        # 直接回复单张或多张（但不会超过3）
        msg = Message()
        for img in images:
            msg += MessageSegment.image(img)
        await bot.send(event, msg, reply_message=reply)
    else:
        # 合并转发
        try:
            bot_info = await bot.get_login_info()
            nickname = bot_info.get("nickname", "Malody预览")
        except Exception:
            nickname = "预览"
        nodes = []
        for img in images:
            node = MessageSegment.node(
                user_id=event.self_id,
                nickname=nickname,
                content=Message(MessageSegment.image(img))
            )
            nodes.append(node)
        await bot.send_forward_msg(
            user_id=event.user_id,
            group_id=event.group_id,
            messages=nodes
        )

# ========================= 文件获取（修改支持 .mc/.mcz） =========================
async def get_file_from_reply(bot: Bot, event: MessageEvent) -> Optional[Tuple[Path, str]]:
    """从回复获取文件，返回 (路径, 类型) 或 None。类型: 'osu' 或 'mc'"""
    if not event.reply:
        return None

    file_name = None
    file_id = None
    file_url = None
    for seg in event.reply.message:
        if seg.type == "file":
            file_name = seg.data.get("name") or seg.data.get("filename") or seg.data.get("file")
            file_id = seg.data.get("file_id")
            file_url = seg.data.get("url")
            break

    if not file_name or not file_id:
        return None

    file_lower = file_name.lower()
    if not (file_lower.endswith(".osu") or file_lower.endswith(".mc") or file_lower.endswith(".mcz")):
        logger.info(f"[预览] 不支持的文件格式: {file_name}")
        return None

    logger.info(f"[预览] 收到文件: {file_name}")

    if not file_url:
        try:
            file_info = await bot.get_file(file_id=file_id)
            file_url = file_info.get("url")
            local_file = file_info.get("file")
            if not file_url and local_file:
                file_url = local_file
        except Exception as e:
            logger.error(f"获取文件信息失败: {e}")
            return None

    if not file_url:
        logger.error("未获取到文件下载链接")
        return None

    try:
        ext = os.path.splitext(file_name)[1]
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp_file:
            tmp_path = Path(tmp_file.name)

        if Path(file_url).is_file():
            shutil.copy(file_url, tmp_path)
        elif file_url.startswith(("http://", "https://")):
            async with httpx.AsyncClient() as client:
                resp = await client.get(file_url, timeout=120.0, follow_redirects=True)
                resp.raise_for_status()
                tmp_path.write_bytes(resp.content)
        else:
            logger.error("无法识别的文件链接格式")
            os.unlink(tmp_path)
            return None

        # 根据后缀决定类型
        if file_lower.endswith((".mc", ".mcz")):
            logger.info("[预览] 检测到 MC 文件，使用自定义渲染")
            return tmp_path, "mc"
        else:
            logger.info(f"[预览] .osu 文件已保存至: {tmp_path}")
            return tmp_path, "osu"

    except Exception as e:
        logger.error(f"下载文件失败: {e}")
        if 'tmp_path' in locals():
            os.unlink(tmp_path)
        return None

# ========================= 预览命令 =========================
generate_preview = on_command("预览", aliases={"preview", "完整预览"}, priority=1, block=True)

@generate_preview.handle(parameterless=[split_msg()])
async def _(bot: Bot, event: MessageEvent, state: T_State):
    logger.info("======== 进入预览命令 ========")
    cmd_text = event.get_plaintext().strip()
    cid = None
    for token in cmd_text.split():
        if token.lower().startswith("c") and token[1:].isdigit():
            cid = int(token[1:])
            break
        # 解析 --effect 和 --group
    # 默认开启 effect 和 group，通过 --noeffect / --nogroup 关闭
    show_effects = '--noeffect' not in cmd_text
    show_groups = '--nogroup' not in cmd_text

    # 解析 --scale 参数，例如 --scale 1.5
    import re
    scale_match = re.search(r'--scale\s+([\d.]+)', cmd_text)
    scale = 1.0
    if scale_match:
        try:
            scale = float(scale_match.group(1))
        except ValueError:
            pass

    # ========== 分支 1：回复文件 ==========
    file_result = await get_file_from_reply(bot, event)
    if file_result:
        local_path, file_type = file_result
        try:
            if file_type == "mc":
                # MC 分支（不再转换为 osu）
                if local_path.suffix.lower() == ".mcz":
                    # 大小限制 50MB
                    if local_path.stat().st_size > 50 * 1024 * 1024:
                        # 不发送失败提示，保持原风格
                        return
                    extract_dir = Path(tempfile.mkdtemp())
                    try:
                        with zipfile.ZipFile(local_path, 'r') as zf:
                            zf.extractall(extract_dir)
                        mc_files = sorted(extract_dir.rglob("*.mc"))
                        if not mc_files:
                            logger.warning("在 .mcz 中未找到 .mc 文件")
                            return
                        images = []
                        for mc in mc_files:
                            try:
                                pic = render_mc_preview(mc, show_events=show_effects, show_groups=show_groups, scale=scale)
                                images.append(pic)
                            except Exception as e:
                                logger.warning(f"渲染 {mc.name} 失败: {e}")
                        if not images:
                            return
                        await send_mc_images(bot, event, images, reply=False)
                        await UniMessage.finish()
                    finally:
                        shutil.rmtree(extract_dir, ignore_errors=True)
                else:
                    # 单个 .mc 文件
                    logger.info("已执行一步")
                    pic = render_mc_preview(local_path, show_events=show_effects, show_groups=show_groups, scale=scale)
                    await UniMessage.image(raw=pic).finish(reply_to=False)

            else:  # file_type == 'osu'
                # 原 osu 处理逻辑
                if count_hitobjects(local_path) == -1:
                    return
                mode = state.get("mode", "0")
                if not (0 <= int(mode) <= 3):
                    return
                bmid, bmsid = None, None
                if mode in ("0", "2"):
                    bmid, bmsid = extract_ids_from_osu(local_path)
                    if bmid is None or bmsid is None:
                        return
                if mode == "3":
                    is_full = state["_prefix"]["command"][0] == "完整预览"
                    pic = await generate_preview_pic(local_path, is_full)
                elif mode == "2":
                    pic = await draw_cath_preview(int(bmid), bmsid, state.get("mods"))
                elif mode == "1":
                    beatmap = parse_map(local_path)
                    pic = map_to_image(beatmap)
                else:  # mode == "0"
                    pic = await draw_osu_preview(int(bmid), bmsid)
                await UniMessage.image(raw=pic).finish(reply_to=False)

        except Exception as e:
            logger.error(f"本地文件预览失败: {e}")
        finally:
            if local_path.exists():
                os.unlink(local_path)
        return

    # ========== 分支 2：Malody CID ==========
    if cid is not None:
        try:
            mc_tmp = await download_by_cid(cid, CACHE_DIR)
        except Exception as e:
            logger.error(f"下载 Malody CID {cid} 失败: {e}")
            return
        if not mc_tmp or not mc_tmp.exists():
            return

        extract_dir = None
        try:
            if mc_tmp.suffix.lower() == ".mcz":
                if mc_tmp.stat().st_size > 50 * 1024 * 1024:
                    return
                extract_dir = Path(tempfile.mkdtemp())
                with zipfile.ZipFile(mc_tmp, 'r') as zf:
                    zf.extractall(extract_dir)
                mc_files = sorted(extract_dir.rglob("*.mc"))
                if not mc_files:
                    return
                images = []
                for mc in mc_files:
                    try:
                        pic = render_mc_preview(mc, show_events=show_effects, show_groups=show_groups, scale=scale)
                        images.append(pic)
                    except Exception as e:
                        logger.warning(f"渲染 {mc.name} 失败: {e}")
                if not images:
                    return
                await send_mc_images(bot, event, images, reply=False)
                await UniMessage.finish()
            else:
                pic = render_mc_preview(mc_tmp, show_events=show_effects, show_groups=show_groups, scale=scale)
                await UniMessage.image(raw=pic).finish(reply_to=False)
        except Exception as e:
            logger.error(f"Malody MC 预览失败: {e}")
        finally:
            if mc_tmp and mc_tmp.exists():
                mc_tmp.unlink()
            if extract_dir:
                shutil.rmtree(extract_dir, ignore_errors=True)
        return

    # ========== 分支 3：osu! bid ==========
    osu_id = state["target"]
    if not osu_id or not osu_id.isdigit():
        return

    try:
        data = await osu_api("map", map_id=int(osu_id))
    except NetworkError as e:
        logger.error(f"查找 map_id:{osu_id} 失败: {e}")
        return

    mode = state["mode"]
    if mode == "3":
        osu = await download_osu(data["beatmapset_id"], int(osu_id))
        if count_hitobjects(osu) == -1:
            return
        is_full = state["_prefix"]["command"][0] == "完整预览"
        pic = await generate_preview_pic(osu, is_full)
    elif mode == "2":
        osu = await download_osu(data["beatmapset_id"], int(osu_id))
        if count_hitobjects(osu) == -1:
            return
        pic = await draw_cath_preview(int(osu_id), data["beatmapset_id"], state.get("mods"))
    elif mode == "1":
        osu = await download_osu(data["beatmapset_id"], int(osu_id))
        if count_hitobjects(osu) == -1:
            return
        beatmap = parse_map(osu)
        pic = map_to_image(beatmap)
    elif mode == "0":
        osu = await download_osu(data["beatmapset_id"], int(osu_id))
        if count_hitobjects(osu) == -1:
            return
        pic = await draw_osu_preview(int(osu_id), data["beatmapset_id"])
    else:
        return

    await UniMessage.image(raw=pic).finish(reply_to=False)
