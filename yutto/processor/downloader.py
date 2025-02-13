import asyncio
import functools
import os
from typing import Any, Optional

import aiohttp

from yutto.media.quality import audio_quality_map, video_quality_map
from yutto.processor.filter import filter_none_value, select_audio, select_video
from yutto.processor.progressbar import show_progress
from yutto.typing import AudioUrlMeta, VideoUrlMeta, EpisodeData, DownloaderOptions
from yutto.utils.asynclib import CoroutineTask, parallel_with_limit
from yutto.utils.console.colorful import colored_string
from yutto.utils.console.logger import Badge, Logger
from yutto.utils.danmaku import write_danmaku
from yutto.utils.fetcher import Fetcher
from yutto.utils.ffmpeg import FFmpeg
from yutto.utils.file_buffer import AsyncFileBuffer
from yutto.utils.subtitle import write_subtitle


def slice_blocks(
    start: int, total_size: Optional[int], block_size: Optional[int] = None
) -> list[tuple[int, Optional[int]]]:
    """生成分块后的 (start, size) 序列

    Args:
        start (int): 总起始位置
        total_size (Optional[int]): 需要分块的总大小
        block_size (Optional[int], optional): 每块的大小. Defaults to None.

    Returns:
        list[tuple[int, Optional[int]]]: 分块大小序列，使用元组组织，格式为 (start, size)
    """
    if total_size is None:
        return [(0, None)]
    if block_size is None:
        return [(0, total_size - 1)]
    assert start <= total_size, "起始地址（{}）大于总地址（{}）".format(start, total_size)
    offset_list: list[tuple[int, Optional[int]]] = [(i, block_size) for i in range(start, total_size, block_size)]
    if (total_size - start) % block_size != 0:
        offset_list[-1] = (
            start + (total_size - start) // block_size * block_size,
            total_size - start - (total_size - start) // block_size * block_size,
        )
    return offset_list


def mix_list(*l_list: list[Any]) -> list[Any]:
    """将多个 list 「均匀」地合并到一个 list

    # example

    ```
    l_list = [
        [1, 2, 3, 4, 5],
        [6, 7, 8],
        [9, 10, 11, 12]
    ]
    mix_list(l_list)
    # [1, 6, 9, 2, 7, 10, 3, 8, 11, 4, 12, 5]
    ```
    """
    results: list[Any] = []
    for i in range(max([len(l) for l in l_list])):
        for l in l_list:
            if i < len(l):
                results.append(l[i])
    return results


def show_videos_info(videos: list[VideoUrlMeta], selected: int):
    """显示视频详细信息"""
    if not videos:
        Logger.info("不包含任何视频流")
        return
    Logger.info(f"共包含以下 {len(videos)} 个视频流：")
    for i, video in enumerate(videos):
        log = "{}{:2} [{:^4}] [{:>4}x{:<4}] <{:^8}> #{}".format(
            "*" if i == selected else " ",
            i,
            video["codec"].upper(),
            video["width"],
            video["height"],
            video_quality_map[video["quality"]]["description"],
            len(video["mirrors"]) + 1,
        )
        if i == selected:
            log = colored_string(log, fore="blue")
        Logger.info(log)


def show_audios_info(audios: list[AudioUrlMeta], selected: int):
    """显示音频详细信息"""
    if not audios:
        Logger.info("不包含任何音频流")
        return
    Logger.info(f"共包含以下 {len(audios)} 个音频流：")
    for i, audio in enumerate(audios):
        log = "{}{:2} [{:^4}] <{:^8}>".format(
            "*" if i == selected else " ", i, audio["codec"].upper(), audio_quality_map[audio["quality"]]["description"]
        )
        if i == selected:
            log = colored_string(log, fore="magenta")
        Logger.info(log)


async def download_video_and_audio(
    session: aiohttp.ClientSession,
    video: Optional[VideoUrlMeta],
    video_path: str,
    audio: Optional[AudioUrlMeta],
    audio_path: str,
    options: DownloaderOptions,
):
    """下载音视频"""

    buffers: list[Optional[AsyncFileBuffer]] = [None, None]
    sizes: list[Optional[int]] = [None, None]
    task_funcs: list[list[CoroutineTask]] = []
    if video is not None:
        vbuf = await AsyncFileBuffer.create(video_path, overwrite=options["overwrite"])
        vsize = await Fetcher.get_size(session, video["url"])
        vtask_funcs = [
            Fetcher.download_file_with_offset(session, video["url"], video["mirrors"], vbuf, offset, block_size)
            for offset, block_size in slice_blocks(vbuf.written_size, vsize, options["block_size"])
        ]
        task_funcs.append(vtask_funcs)
        buffers[0], sizes[0] = vbuf, vsize

    if audio is not None:
        abuf = await AsyncFileBuffer.create(audio_path, overwrite=options["overwrite"])
        asize = await Fetcher.get_size(session, audio["url"])
        atask_funcs = [
            Fetcher.download_file_with_offset(session, audio["url"], audio["mirrors"], abuf, offset, block_size)
            for offset, block_size in slice_blocks(abuf.written_size, asize, options["block_size"])
        ]
        task_funcs.append(atask_funcs)
        buffers[1], sizes[1] = abuf, asize

    tasks = parallel_with_limit(mix_list(*task_funcs), num_workers=options["num_workers"])
    tasks.append(asyncio.create_task(show_progress(filter_none_value(buffers), sum(filter_none_value(sizes)))))

    Logger.info("开始下载……")
    for task in tasks:
        await task
    Logger.info("下载完成！")

    if video is not None:
        await vbuf.close()
    if audio is not None:
        await abuf.close()


def merge_video_and_audio(
    video: Optional[VideoUrlMeta],
    video_path: str,
    audio: Optional[AudioUrlMeta],
    audio_path: str,
    output_path: str,
    options: DownloaderOptions,
):
    """合并音视频"""

    ffmpeg = FFmpeg()
    Logger.info("开始合并……")

    if video is not None and video["codec"] == options["video_save_codec"]:
        options["video_save_codec"] = "copy"
    if audio is not None and audio["codec"] == options["audio_save_codec"]:
        options["audio_save_codec"] = "copy"

    args_list: list[list[str]] = [
        ["-i", video_path] if video is not None else [],
        ["-i", audio_path] if audio is not None else [],
        ["-vcodec", options["video_save_codec"]] if video is not None else [],
        ["-acodec", options["audio_save_codec"]] if video is not None else [],
        ["-y", output_path],
    ]

    ffmpeg.exec(functools.reduce(lambda prev, cur: prev + cur, args_list))
    Logger.info("合并完成！")

    if video is not None:
        os.remove(video_path)
    if audio is not None:
        os.remove(audio_path)


async def process_video_download(
    session: aiohttp.ClientSession,
    episode_data: EpisodeData,
    options: DownloaderOptions,
):
    """处理单个视频下载任务，包含弹幕、字幕的存储"""

    videos = episode_data["videos"]
    audios = episode_data["audios"]
    subtitles = episode_data["subtitles"]
    danmaku = episode_data["danmaku"]
    output_dir = episode_data["output_dir"]
    filename = episode_data["filename"]

    Logger.info("开始处理视频 {}".format(filename))
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)
    output_path_no_ext = os.path.join(output_dir, filename)
    video_path = output_path_no_ext + "_video.m4s"
    audio_path = output_path_no_ext + "_audio.m4s"

    video = select_video(videos, options["require_video"], options["video_quality"], options["video_download_codec"])
    audio = select_audio(audios, options["require_audio"], options["audio_quality"], options["audio_download_codec"])

    # 显示音视频详细信息
    show_videos_info(videos, videos.index(video) if video is not None else -1)
    show_audios_info(audios, audios.index(audio) if audio is not None else -1)

    output_format = ".mp4" if video is not None else ".aac"
    output_path = output_path_no_ext + output_format
    if os.path.exists(output_path):
        if not options["overwrite"]:
            Logger.info("文件 {} 已存在".format(filename))
            return
        else:
            Logger.info("文件已存在，因启用 overwrite 选项强制删除……")
            os.remove(output_path)

    if video is None and audio is None:
        Logger.warning("没有音视频需要下载")
        return

    # 保存字幕
    if subtitles:
        for subtitle in subtitles:
            write_subtitle(subtitle["lines"], output_path, subtitle["lang"])
        Logger.custom(
            "{} 字幕已全部生成".format(", ".join([subtitle["lang"] for subtitle in subtitles])),
            badge=Badge("字幕", fore="black", back="cyan"),
        )

    # 保存弹幕
    if danmaku["data"]:
        write_danmaku(
            danmaku,
            output_path,
            video["height"] if video is not None else 0,
            video["width"] if video is not None else 0,
        )
        Logger.custom("{} 弹幕已生成".format(danmaku["save_type"]).upper(), badge=Badge("弹幕", fore="black", back="cyan"))

    # 下载视频 / 音频
    await download_video_and_audio(session, video, video_path, audio, audio_path, options)

    # 合并视频 / 音频
    merge_video_and_audio(video, video_path, audio, audio_path, output_path, options)
