#!/usr/bin/env python3
"""Search video resources (BT/magnet) and rank index metadata, not verified media.

The upstream index is a raw full-text BT index: it is fast and huge, but noisy.
Un-ranked output mixes ad-farm repacks, cam rips, unrelated fuzzy matches and
porn packs into results for ordinary movie titles. This script does the boring,
deterministic work -- fetch, paginate, de-duplicate, parse release names, score,
filter -- so the agent can spend its attention on picking the right release for
the user instead of re-deriving heuristics every session.

Usage:
    btsearch.py "沙丘 2" --type movie --quality 4k
    btsearch.py "漫长的季节 第3集" --type tv
    btsearch.py "Oppenheimer" --json

Exit codes: 0 ok (even with zero hits), 2 invalid input or network/API failure.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = os.environ.get("BTBIRD_BASE", "https://www.btbird221101.link").rstrip("/")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
MAX_LIMIT = 50  # API rejects >50 (returns an empty page at 100, invalid_argument at 200)
GB = 1024 ** 3

# --- release-name vocabulary -------------------------------------------------

RESOLUTION_PATTERNS = [
    # "4K修复" means "restored from film", not "2160 lines" -- those releases are
    # routinely 1080p encodes, so the label must not be read as a resolution.
    (2160, re.compile(r"(2160[pi]|4k(?!\s*(修复|修複|修複版|remaster))(?![a-z])|uhd|ultrahd)", re.I)),
    (1080, re.compile(r"(1080[pi]|fhd|蓝光1080)", re.I)),
    (720, re.compile(r"(720[pi]|hd720)", re.I)),
    (480, re.compile(r"(480p|360p)", re.I)),
]
BAD_SOURCE = re.compile(r"(枪版|抢先版|\bts\b|\btc\b|\bcam\b|hdts|hdtc|预告|片花|花絮|sample|访谈)", re.I)
AD_NOISE = re.compile(r"(www\.|\.com|\.net|\.org|\.cc|论坛|最新地址|发布页|更多.{0,6}请访问|【.{0,12}网】)", re.I)
HDR_BONUS = re.compile(r"(dolby.?vision|杜比视界|dovi|hdr10\+?|hdr)", re.I)
REMUX = re.compile(r"(remux|原盘|原盤)", re.I)
SUB_HINT = re.compile(r"(中字|简繁|中英字幕|简体字幕|繁体字幕|\bchs\b|\bcht\b|字幕)", re.I)
EPISODE_TOKEN = re.compile(
    r"(第\s*\d{1,3}\s*[集话話]|[Ee][Pp]?\d{1,3}(?!\d)|\bS\d{1,2}E\d{1,3}\b|\b\d{1,3}\s*[-~]\s*\d{1,3}\b|全\s*\d{1,3}\s*[集话話]|合集|季)",
    re.I,
)
YEAR = re.compile(r"(19[3-9]\d|20[0-4]\d)")

# Adult-labelled entries are noisy on ordinary title searches. Keep them only
# when the caller explicitly enables adult mode. Even then, exclude terms that
# suggest minors or non-consensual/hidden-camera material.
ADULT = re.compile(
    r"(国产av|\bav\b|无码|無碼|有码|有碼|色情|情色|做爱|做愛|口交|肛交|自慰|淫|骚|騷|裸聊|"
    r"censored|uncensored|jav|xxx|porn|milf|hentai|91制片|麻豆|swag|onlyfans|"
    r"fc2|caribbeancom|heyzo|一本道|痴汉|癡漢|巨乳|肉便器|调教|調教|约炮|約炮)",
    re.I,
)
UNSAFE_ADULT = re.compile(
    r"(萝莉|蘿莉|幼女|未成年|小学生|初中生|儿童色情|child\s*porn|cp\b|迷奸|迷姦|强奸|強姦|"
    r"强迫|強迫|偷拍|盗摄|盜攝|hidden\s*cam|revenge\s*porn|兽交|獸交|bestiality)",
    re.I,
)

VIDEO_EXT = re.compile(r"\.(mp4|mkv|avi|ts|m2ts|rmvb|wmv|mov|iso|flv)$", re.I)


# --- fetching ----------------------------------------------------------------


def _ssl_context():
    """Verify TLS, honoring an explicit CA file before optional certifi roots."""
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None,
                                          capath=os.environ.get("SSL_CERT_DIR") or None)
    try:
        import certifi  # type: ignore
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


class SearchError(RuntimeError):
    """Upstream index failed in a way the caller may be able to route around."""


def api_search(words: str, limit: int, offset: str | None, base: str, timeout: int = 25) -> dict:
    payload = {"biz_id": urllib.parse.urlsplit(base).netloc, "limit": limit, "words": words}
    if offset:
        payload["offset"] = offset
    req = urllib.request.Request(
        f"{base}/api/search",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "accept": "application/json",
            "origin": base,
            "referer": f"{base}/search",
            "user-agent": UA,
        },
        method="POST",
    )
    try:
        ctx = _ssl_context()
    except (OSError, ssl.SSLError) as exc:
        raise SearchError(
            f"无法加载可信 CA: {exc}。请检查 SSL_CERT_FILE/SSL_CERT_DIR 或安装 certifi；不会跳过证书校验。"
        ) from exc
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict):
                raise SearchError("API 返回格式错误：应为 JSON 对象。")
            if data.get("error"):
                raise SearchError(f"API 报错: {data.get('error_description') or data['error']}")
            if not isinstance(data.get("bts") or [], list) or any(
                not isinstance(item, dict) for item in (data.get("bts") or [])
            ):
                raise SearchError("API 返回格式错误：bts 应为对象列表。")
            return data
        except (urllib.error.URLError, ssl.SSLError, TimeoutError,
                json.JSONDecodeError, UnicodeDecodeError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ssl.SSLError):
                raise SearchError(
                    f"TLS 校验/连接失败: {reason}。请检查站点证书、本机时钟及 CA 配置，"
                    "可用 SSL_CERT_FILE 指定可信 CA 或安装 certifi；不会跳过校验或重试此错误。"
                ) from exc
            last_err = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise SearchError(
        f"搜索接口不可达: {last_err}\n"
        f"请检查网络及站点 {base}，仅在确认新地址可信后用 BTBIRD_BASE 或 --base 覆盖。"
    )


def collect(words: str, pages: int, base: str,
            warnings: list[str] | None = None) -> tuple[list[dict], int]:
    """Keep earlier pages on failure, visibly reporting incomplete pagination."""
    seen: set[str] = set()
    cursors: set[str] = set()
    out: list[dict] = []
    total = 0
    offset = None
    for page in range(pages):
        try:
            data = api_search(words, MAX_LIMIT, offset, base)
            count = data.get("total_count")
            if count is not None and (not isinstance(count, int) or count < 0):
                raise SearchError("API 返回格式错误：total_count 应为非负整数。")
            next_offset = data.get("next_offset")
            if next_offset is not None and not isinstance(next_offset, str):
                raise SearchError("API 返回格式错误：next_offset 应为字符串。")
        except SearchError as exc:
            if page == 0:
                raise
            message = f"第 {page + 1} 页获取失败，仅保留此前结果：{exc}"
            if warnings is not None:
                warnings.append(message)
            else:
                print(f"[btsearch] {message}", file=sys.stderr)
            break
        total = count if count is not None else total
        bts = data.get("bts") or []
        for item in bts:
            h = item.get("info_hash")
            if not isinstance(h, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", h):
                continue
            h = h.upper()
            if h not in seen:
                seen.add(h)
                out.append(item)
        if not bts or not next_offset:
            break
        if next_offset in cursors:
            message = "API 重复返回分页游标，已停止分页；结果可能不完整。"
            if warnings is not None:
                warnings.append(message)
            else:
                print(f"[btsearch] {message}", file=sys.stderr)
            break
        cursors.add(next_offset)
        offset = next_offset
    return out, total


# --- parsing / scoring -------------------------------------------------------


# Attribute words the *index* does not index usefully: including them in the
# query collapses recall (e.g. "沙丘2 4k" returns 5 hits, "沙丘2" returns 213),
# because the backend matches them literally against release names. They are
# far more useful as ranking signals, so they are stripped from the query and
# re-applied as preferences.
# `\b` is useless here: Python treats CJK as word characters, so "4K修复版"
# has no boundary after "4K" and the attribute survives into the query.
# Latin boundaries are therefore spelled out against latin neighbours only.
_A = r"(?<![A-Za-z0-9])"
_Z = r"(?![A-Za-z0-9])"
STRIP_FROM_QUERY = re.compile(
    rf"({_A}(4k|2160p?|1080[pi]?|720[pi]?|uhd|hdr10?\+?|dovi|dolby\s?vision|blu-?ray|bdrip|"
    rf"bdremux|remux|web-?dl|webrip|x26[45]|h\.?26[45]){_Z}"
    r"|蓝光|藍光|超清|高清|原盘|原盤|中字|中文字幕|字幕|国语|國語|粤语|粵語|双语|雙語|"
    r"修复版|修複版|修复|修複|重制版|重製版|清晰|画质|畫質|"
    r"资源|資源|免费|免費|在线|線上|下载|下載|完整版|求片)",
    re.I,
)
# Resolution hints are read from the raw query, but "4K修复" is a restoration
# label rather than a resolution, so it must not raise the bar to 2160p.
_RESTORATION = re.compile(r"4k[\s._-]*(修复|修複|restor|remaster)", re.I)
QUERY_RES_HINT = [
    (2160, re.compile(rf"{_A}(4k|2160p?|uhd){_Z}", re.I)),
    (1080, re.compile(rf"{_A}1080[pi]?{_Z}", re.I)),
    (720, re.compile(rf"{_A}720[pi]?{_Z}", re.I)),
]


EPISODE_REQUEST = re.compile(
    r"(第\s*(\d{1,3})\s*[集话話]|\bS\d{1,2}E(\d{1,3})\b|\b[Ee][Pp](\d{1,3})\b|"
    r"(\d{1,3})\s*集(?!数))",
    re.I,
)


class Query:
    """A parsed request: what to send upstream vs. what to rank by."""

    __slots__ = ("raw", "words", "tokens", "year", "resolution", "episode")

    def __init__(self, raw, words, tokens, year, resolution, episode):
        self.raw, self.words, self.tokens = raw, words, tokens
        self.year, self.resolution, self.episode = year, resolution, episode


def parse_query(query: str) -> Query:
    """Split a human phrasing into upstream search words and ranking preferences.

    Keeping the search words close to the bare title is what makes recall good;
    the stripped attributes are not lost, they become scoring preferences.
    An episode request is stripped too, because season packs -- the thing worth
    returning -- are named "全12集", never "第3集".
    """
    hint_src = _RESTORATION.sub(" ", query)
    want_res = 0
    for value, pattern in QUERY_RES_HINT:
        if pattern.search(hint_src):
            want_res = value
            break

    episode = 0
    m = EPISODE_REQUEST.search(query)
    if m:
        found = next((g for g in m.groups()[1:] if g), None)
        if found:
            episode = int(found)

    stripped = EPISODE_REQUEST.sub(" ", query)
    stripped = re.sub(r"\s+", " ", STRIP_FROM_QUERY.sub(" ", stripped)).strip(" -_.·")
    words = stripped or query.strip()

    raw = re.findall(r"[A-Za-z0-9]+", words) + re.findall(r"[\u4e00-\u9fff]+", words)
    year = ""
    tokens = []
    for t in raw:
        low = t.lower()
        if not year and re.fullmatch(r"(19[3-9]\d|20[0-4]\d)", low):
            year = low
            continue
        tokens.append(low)
    return Query(query, words, tokens or [t.lower() for t in raw], year, want_res, episode)



def resolution_of(text: str) -> int:
    text = _RESTORATION.sub(" ", text)
    for value, pattern in RESOLUTION_PATTERNS:
        if pattern.search(text):
            return value
    return 0


EP_MARK = re.compile(
    r"(第\s*(\d{1,3})\s*[集话話]|\bS\d{1,2}E(\d{1,3})\b|\b[Ee][Pp]?(\d{1,3})(?!\d)|"
    r"(?<![\d.])(\d{1,3})(?=\s*\.[a-z0-9]{2,4}$))",
    re.I,
)


def _videos(item: dict) -> list[tuple[str, int]]:
    out = []
    for f in item.get("sub_files") or []:
        path = f.get("file_path", "")
        if f.get("mime_type") == "video" or VIDEO_EXT.search(path):
            try:
                out.append((path, int(f.get("file_size") or 0)))
            except (TypeError, ValueError):
                out.append((path, 0))
    return out


def episode_span(item: dict) -> tuple[int, bool]:
    """Count observed episode markers or title declarations, never total files.

    The flag means the index listing may be incomplete, not that a season is
    complete. Declarations in release names are unverified metadata.
    """
    videos = _videos(item)

    nums = set()
    for path, _ in videos:
        base = path.rsplit("/", 1)[-1]
        for m in EP_MARK.finditer(base):
            nums.add(int(next(g for g in m.groups()[1:] if g)))
    listed = len(nums)

    name = item.get("name", "")
    declared = 0
    m = re.search(r"全\s*(\d{1,3})\s*[集话話]", name)
    if m:
        declared = int(m.group(1))
    r = re.search(r"(?<![\d.])(\d{1,3})\s*[-~]\s*(\d{1,3})(?![\d.])", name)
    if r and int(r.group(2)) > int(r.group(1)):
        declared = max(declared, int(r.group(2)) - int(r.group(1)) + 1)

    files = item.get("sub_files") or []
    truncated = len(files) >= 10 or (item.get("file_num") or 0) > len(files)
    return max(declared, listed), truncated


DISC_STRUCTURE = re.compile(r"(BDMV|VIDEO_TS|CERTIFICATE|STREAM[/\\])", re.I)


def junk_bundle(item: dict) -> bool:
    """Flag several small video clips for review, not as proof of bait.

    Disc structures are exempt because menu clips are expected there.
    """
    videos = _videos(item)
    if any(DISC_STRUCTURE.search(p) for p, _ in videos):
        return False
    return sum(1 for _, s in videos if 0 < s < 50 * 1024 * 1024) >= 3


def classify(item: dict) -> str:
    """movie | tv — a release-name heuristic for display and ranking."""
    name = item.get("name", "")
    files = item.get("file_num") or 1
    episodes = episode_span(item)[0]
    if EPISODE_TOKEN.search(name) and files >= 2:
        return "tv"
    if episodes >= 3:
        return "tv"
    return "movie"


def describe(item: dict) -> dict:
    info_hash = item.get("info_hash")
    if not isinstance(info_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", info_hash):
        raise SearchError("API 条目的 info_hash 必须是 40 位十六进制字符串。")
    info_hash = info_hash.upper()
    name = item.get("name", "")
    blob = name + " " + " ".join(f.get("file_path", "") for f in (item.get("sub_files") or [])[:8])
    size = int(item.get("file_size") or 0)
    size_gb = round(size / GB, 2)
    kind = classify(item)
    eps, eps_est = episode_span(item)
    claimed = resolution_of(blob)
    years = YEAR.findall(name)
    return {
        "info_hash": info_hash,
        "name": name,
        "magnet": f"magnet:?xt=urn:btih:{info_hash}",
        "size_bytes": size,
        "size_gb": size_gb,
        "file_num": item.get("file_num") or 0,
        "record_time": item.get("record_time", ""),
        "hot_score": item.get("hot_score") or 0,
        "resolution": claimed,
        "kind": kind,
        "episodes": eps,
        "episodes_estimated": eps_est,
        "year": years[0] if years else "",
        "adult": bool(ADULT.search(blob)),
        "unsafe_adult": bool(UNSAFE_ADULT.search(blob)),
        "bad_source": bool(BAD_SOURCE.search(name)),
        "junk_bundle": junk_bundle(item),
        "hdr": bool(HDR_BONUS.search(blob)),
        "remux": bool(REMUX.search(blob)),
        "subs": bool(SUB_HINT.search(blob)),
        "ad_noise": bool(AD_NOISE.search(name)),
        "sub_files": [f.get("file_path", "") for f in (item.get("sub_files") or [])[:6]],
    }


def title_match(rec: dict, tokens: list[str]) -> float:
    """Fraction of query tokens present in the release name.

    Fuzzy full-text search happily returns releases that merely share one
    character with the query, so explicit coverage is what separates "the
    film the user asked for" from "something with 龙 in the title". Short
    numeric tokens get digit boundaries, otherwise the "2" in "沙丘2" would
    happily match the "2" inside "Dune.2021".
    """
    if not tokens:
        return 1.0
    name = rec["name"].lower()
    hit = 0
    for t in tokens:
        if t.isdigit() and len(t) <= 2:
            if re.search(rf"(?<!\d){re.escape(t)}(?!\d)", name):
                hit += 1
        elif t in name:
            hit += 1
    return hit / len(tokens)


def score(rec: dict, tokens: list[str], want_year: str, want_type: str, want_res: int) -> float:
    s = 0.0
    coverage = rec["coverage"]
    s += coverage * 45
    if coverage < 0.5:
        s -= 25  # probably a different title that merely shares a keyword

    if want_year:
        if want_year in rec["name"]:
            s += 12
        elif rec["year"] and rec["year"] != want_year:
            s -= 15  # a different entry in the same franchise, e.g. 1992 vs 2024

    res = rec["resolution"]
    if want_res:
        s += 28 if res >= want_res else -12 if res else -4
    else:
        s += {2160: 20, 1080: 18, 720: 8, 480: -6, 0: 0}[res]

    s += 8 * math.log10(max(0, rec["hot_score"]) + 1)  # index popularity, not seeders or availability

    if want_type != "any" and rec["kind"] != want_type:
        s -= 18
    if rec["kind"] == "tv":
        s += min(rec["episodes"], 20) * 0.6  # prefer more observed/declared episodes, not proven completeness
    else:
        gb = rec["size_gb"]
        if gb < 0.3:
            s -= 20          # size preference only; runtime and media quality are unknown
        elif gb > 120:
            s -= 10          # deprioritize unusually large movie entries
        elif 1.5 <= gb <= 80:
            s += 6

    if rec["hdr"]:
        s += 4
    if rec["remux"]:
        s += 5
    if rec["subs"]:
        s += 4
    if rec["ad_noise"]:
        s -= 3
    if rec["bad_source"]:
        s -= 35
    if rec["junk_bundle"]:
        s -= 14  # small-clip heuristic; does not establish unrelated content
    if rec["record_time"][:4].isdigit():
        s += min(int(rec["record_time"][:4]) - 2015, 8) * 0.4

    return round(s, 2)


# --- output ------------------------------------------------------------------


def human(rows: list[dict], q: Query, used_words: str, total: int, shown_note: str,
          show_files: bool = False) -> str:
    head = f'查询: "{q.raw}"' + (f'  →  检索词 "{used_words}"' if used_words != q.raw.strip() else "")
    if not rows:
        return (
            f"{head}\n{shown_note}；已取得结果中无可用条目。\n"
            "换个说法再试：用原名或英文名、去掉年份、只保留核心片名（如「神探狄仁杰第四部」→「神探狄仁杰」）。"
        )
    lines = [f"{head}   索引命中 {total} 条，{shown_note}",
             "说明：分辨率、来源和字幕均为名称标记，未经媒体核验；索引热度不代表做种数或可下载性。"]
    if q.episode:
        lines.append(
            f"注：你要的是第 {q.episode} 集。下列条目可能是单集、合集或不完整资源；"
            "请核对文件清单是否包含目标集，不能据此保证整季齐全。"
        )
    lines.append("")
    for i, r in enumerate(rows, 1):
        res = f"{r['resolution']}p 标记" if r["resolution"] else "分辨率未标注"
        tags = []
        if r["adult"]:
            tags.append("成人向标记")
        if r["hdr"]:
            tags.append("HDR/DV标记")
        if r["remux"]:
            tags.append("REMUX/原盘标记")
        if r["subs"]:
            tags.append("字幕标记（未核验）")
        if r["kind"] == "tv":
            tags.append(f"观察/声明 {r['episodes']} 集" + ("（文件清单不完整）" if r["episodes_estimated"] else ""))
        if r["junk_bundle"]:
            tags.append("含多个小视频（需核对）")
        tag = "  ".join(tags)
        lines.append(f"[{i}] {r['name'][:110]}")
        lines.append(
            f"    {res} | {r['size_gb']} GB | {r['file_num']} 文件 | 收录 {r['record_time']}"
            f" | 索引热度 {r['hot_score']} | 评分 {r['score']}"
            + (f" | {tag}" if tag else "")
        )
        lines.append(f"    {r['magnet']}")
        if show_files and r["sub_files"]:
            lines.append("      文件名预览（可能截断，不保证完整）：")
            for path in r["sub_files"][:4]:
                lines.append(f"      · {path[:96]}")
        lines.append("")
    return "\n".join(lines)


def report_error(message: str, json_mode: bool, code: str) -> int:
    if json_mode:
        print(json.dumps({"error": {"code": code, "message": message}, "results": []},
                         ensure_ascii=False, indent=2))
    else:
        print(f"[btsearch] {message}", file=sys.stderr)
    return 2


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        report_error(message, "--json" in sys.argv[1:], "invalid_arguments")
        self.exit(2)


def main() -> int:
    p = ArgumentParser(description="搜索并排序影视 BT 索引元数据（不核验媒体或下载可用性）")
    p.add_argument("query", help="片名、演员或番号关键词，可加年份/画质")
    p.add_argument("-n", "--top", type=int, default=8, help="输出条数 (默认 8)")
    p.add_argument("--pages", type=int, default=2, help="抓取页数，每页 50 条 (默认 2)")
    p.add_argument("--type", choices=["movie", "tv", "any"], default="any", help="资源类型偏好")
    p.add_argument("--quality", choices=["4k", "1080p", "720p", "any"], default="any", help="最低画质偏好")
    p.add_argument("--min-gb", type=float, default=0.0)
    p.add_argument("--max-gb", type=float, default=0.0)
    p.add_argument("--year", default="", help="限定年份；也可直接写进查询词")
    p.add_argument("--loose", action="store_true", help="保留弱相关结果（默认剔除标题几乎不匹配的条目）")
    p.add_argument("--adult", action="store_true",
                   help="仅在用户明确要求成人向内容时启用；普通搜索默认过滤")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.add_argument("--show-files", action="store_true",
                   help="预览索引文件名以核对内容；列表可能截断，不能证明文件真实")
    p.add_argument("--base", default=DEFAULT_BASE, help="站点地址，域名轮换时覆盖")
    args = p.parse_args()
    if not args.query.strip():
        p.error("query 不能为空。")
    if args.top <= 0 or args.pages <= 0:
        p.error("--top 和 --pages 必须是正整数。")
    if any(not math.isfinite(value) or value < 0 for value in (args.min_gb, args.max_gb)):
        p.error("--min-gb 和 --max-gb 必须是有限非负数。")
    if args.max_gb and args.max_gb < args.min_gb:
        p.error("--max-gb 不能小于 --min-gb（0 表示不设上限）。")
    try:
        base = urllib.parse.urlsplit(args.base)
        valid_base = (base.scheme == "https" and base.hostname and base.username is None
                      and base.password is None and base.path in ("", "/")
                      and not base.query and not base.fragment
                      and not any(c.isspace() or ord(c) < 32 for c in args.base)
                      and "\\" not in args.base and base.port != 0)
    except ValueError:
        valid_base = False
    if not valid_base:
        p.error("--base/BTBIRD_BASE 必须是 HTTPS 站点根地址，不含凭据、路径、查询或片段。")
    args.base = args.base.rstrip("/")

    q = parse_query(args.query)
    used_words = q.words

    warnings: list[str] = []
    try:
        raw, total = collect(used_words, args.pages, args.base, warnings)
        if not raw and not warnings and len(q.tokens) > 1:
            # Broaden only a successful empty search, never hide API failures.
            used_words = max(q.tokens, key=len)
            raw, total = collect(used_words, args.pages, args.base, warnings)
    except SearchError as exc:
        return report_error(str(exc), args.json, "search_failed")
    tokens = q.tokens
    want_year = args.year or q.year
    want_res = {"4k": 2160, "1080p": 1080, "720p": 720, "any": q.resolution}[args.quality]
    want_type = args.type
    if want_type == "any" and q.episode:
        want_type = "tv"  # "第3集" is an unambiguous series request

    recs = []
    dropped_adult = dropped_unsafe = dropped_weak = 0
    for item in raw:
        try:
            r = describe(item)
            r["coverage"] = round(title_match(r, tokens), 2)
            r["score"] = score(r, tokens, want_year, want_type, want_res)
        except (SearchError, TypeError, ValueError, AttributeError, OverflowError) as exc:
            return report_error(f"API 条目格式错误: {exc}", args.json, "invalid_response")
        if r["unsafe_adult"]:
            dropped_unsafe += 1
            continue
        if r["adult"] and not args.adult:
            dropped_adult += 1
            continue
        # A fuzzy index returns plenty of titles sharing one token with the
        # query. Showing those as "results" wastes the user's judgement, so
        # they are dropped unless --loose asks for the raw pool.
        if not args.loose and r["coverage"] < 0.5:
            dropped_weak += 1
            continue
        if args.min_gb and r["size_gb"] < args.min_gb:
            continue
        if args.max_gb and r["size_gb"] > args.max_gb:
            continue
        recs.append(r)

    recs.sort(key=lambda r: r["score"], reverse=True)
    rows = recs[:args.top]
    note = f"已去重排序 {len(recs)} 条"
    if dropped_adult:
        note += f"，过滤成人内容 {dropped_adult} 条"
    if dropped_unsafe:
        note += f"，过滤不安全成人内容 {dropped_unsafe} 条"
    if dropped_weak:
        note += f"，剔除弱相关 {dropped_weak} 条"
    for warning in warnings:
        print(f"[btsearch] {warning}", file=sys.stderr)
    if args.json:
        result = {"query": q.raw, "search_words": used_words, "episode_requested": q.episode,
                  "adult_mode": args.adult, "total_count": total,
                  "filtered_adult": dropped_adult, "filtered_unsafe": dropped_unsafe,
                  "filtered_weak": dropped_weak, "results": rows,
                  "partial": bool(warnings), "warnings": warnings}
        if warnings:
            result["error"] = {"code": "partial_search", "message": "部分分页失败或游标重复；结果不完整。"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if warnings:
            note += "，分页中断，仅显示部分结果"
        print(human(rows, q, used_words, total, note, args.show_files))
    return 2 if warnings else 0


if __name__ == "__main__":
    sys.exit(main())
