# -*- coding: utf-8 -*-
"""
cool18 小说抓取核心逻辑（无 GUI，可独立命令行运行 / 单元测试）。

用法:
  python crawler.py analyze  <帖子URL> [--keyword 书名] [--proxy socks5h://127.0.0.1:10808]
  python crawler.py download <帖子URL> [--out 输出目录] [--keyword 书名] [--proxy ...]
                             [--tids 1,2,3 只下载指定tid] [--interval 0.8]
"""
import argparse
import json
import os
import re
import sys
import time
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Callable
from urllib.parse import quote, urljoin

try:
    import requests
except ImportError:
    requests = None
try:
    from bs4 import BeautifulSoup, Tag
except ImportError:
    BeautifulSoup = None
    Tag = object

SITE = "https://www.cool18.com"
__version__ = "1.0.1"
SEARCH_AREA = "全成人区搜索"
DEFAULT_PROXY = "socks5h://127.0.0.1:10808"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 全角 -> 半角（数字/括号/连字符/空格）
_FW = {ord(c): ord(h) for c, h in zip(
    "０１２３４５６７８９（）－～：，．【】",
    "0123456789()-~:,.[][]")}


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Candidate:
    tid: str
    title: str = ""
    date: str = ""                 # 发帖时间 2026-07-08 20:37
    href: str = ""                 # 帖子完整 URL
    board: str = ""                # 板块，如 禁忌书屋
    source: str = ""               # 目录 / 搜索 / 目录+搜索 / 起始帖
    start: Optional[int] = None    # 卷起始章节号
    end: Optional[int] = None
    part: Optional[int] = None     # 同一卷号内的 上/中/下（1/2/3），无则 None
    disp: str = ""                 # 卷号显示覆盖（上/中/下）
    checked: bool = True
    status: str = ""               # 状态说明（重复已排除 / 重叠等）

    def range_text(self) -> str:
        if self.disp:
            return self.disp
        if self.start is None:
            return "无卷号"
        if self.start == self.end:
            return f"({self.start})"
        return f"({self.start}-{self.end})"

    def sort_key(self):
        if self.start is None:
            return (1, 0, 0, self.part or 0, self.date or "")
        return (0, self.start, self.end or 0, self.part or 0, self.date or "")


# --------------------------------------------------------------------------- #
# 网络
# --------------------------------------------------------------------------- #
class FetchError(Exception):
    pass


class NotFoundError(FetchError):
    """404：帖子已被删除，重试无意义。"""


def make_session(proxy: str = ""):
    if requests is None:
        raise FetchError("缺少 requests 库: pip install requests PySocks beautifulsoup4")
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def fetch(url: str, session=None, tries: int = 3, timeout: int = 30,
          wait: float = 1.5) -> str:
    s = session or make_session()
    last = None
    for i in range(tries):
        try:
            r = s.get(url, timeout=timeout)
            if r.status_code == 404:
                raise NotFoundError(f"帖子不存在(404，可能已被删除): {url}")
            r.raise_for_status()
            r.encoding = "utf-8"
            if "content-section" not in r.text and "search-content" not in r.text:
                # 拿到的是错误页/验证页
                raise FetchError("返回页面异常(可能被限流)")
            return r.text
        except NotFoundError:
            raise                          # 404 不重试
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(wait * (i + 1))
    raise FetchError(f"请求失败 {url} : {last}")


# --------------------------------------------------------------------------- #
# 解析工具
# --------------------------------------------------------------------------- #
def normalize(s: str) -> str:
    s = (s or "").translate(_FW)
    s = re.sub(r"\s+", "", s)
    return s.lower()


def parse_range(title: str):
    t = normalize(title)
    m = re.search(r"[(\[](\d{1,4})\s*[-~]\s*(\d{1,4})", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"[(\[](\d{1,4})(?:\s*[-~]\s*(\d{1,4}))?", t)
    if m:
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        return a, b
    # 标题尾部直接写 1-56 之类
    m = re.search(r"(\d{1,4})\s*[-~]\s*(\d{1,4})", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    # 标题直接写 第二章 / 第12章 这类（无括号卷号）
    m = re.search(rf"第\s*({_NUMCHAR})\s*[章回]", t)
    if m:
        n = cn2int(m.group(1))
        if n is not None:
            return n, n
    return None, None


_PART_MAP = {"上": 1, "中": 2, "下": 3}
_PART_CHAR = {1: "上", 2: "中", 3: "下"}
_PART_ATTACH_RE = re.compile(r"[(\[](上|中|下|\d{1,4}\s*(?:上|中|下))(?:部|集|篇|卷)?[)\]]")


def parse_part(title: str) -> Optional[str]:
    """从标题识别分卷部分：`（7下）`（返回 (7, '下')）或纯 `（上）`（返回 (None,'上')）。"""
    m = _PART_ATTACH_RE.search(normalize(title))
    if not m:
        return None
    token = m.group(1)
    m2 = re.match(r"(\d{1,4})\s*(上|中|下)$", token)
    if m2:
        return (int(m2.group(1)), m2.group(2))
    return (None, token)


def apply_range_or_part(c: "Candidate"):
    """设置卷区间：数字卷号为主，上/中/下 记入 part（兼容 (7)/(7上)/(7下)、(上)(中)(下)）。"""
    c.start, c.end = parse_range(c.title)
    pp = parse_part(c.title)
    if pp:
        pnum, pchar = pp
        c.part = _PART_MAP[pchar]
        if c.start is not None:
            c.disp = f"({c.start}{pchar})"
        else:                       # 纯 上/中/下，无章号：不参与章号对账
            c.disp = pchar


def _merge_intervals(items):
    """合并重叠/相邻区间，返回按起点排序的 [(a,b)]。"""
    out = []
    for a, b in sorted(items):
        if out and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [tuple(x) for x in out]


def coverage_report(cands: List["Candidate"],
                    declared_min: Optional[int] = None,
                    declared_max: Optional[int] = None) -> dict:
    """把“勾选卷”的实际章节区间求并集，找出 [最小..最大/声明总数] 内的缺漏。

    返回 dict:
      max_chapter   已下载勾选卷覆盖到的最大章号
      target_max    对比基准（勾选覆盖 max 与标题声明 max 的较大者）
      n_covered     勾选卷数量（有章号的）
      n_no_range    勾选但无章号（全文/上中下/未核）的卷数
      covered_n     去重后实际覆盖的章节数
      overlap_n     多卷重复覆盖的章节数（下载时按内容再去重）
      missing       缺失章号区间 [(a,b),...]
      complete      是否无缺漏
    """
    checked = [c for c in cands if c.checked]
    iv = [(c.start, c.end) for c in checked
          if c.start is not None and c.end is not None]
    n_no_range = sum(1 for c in checked
                     if c.start is None or c.end is None)
    merged = _merge_intervals(iv)
    covered_n = sum(b - a + 1 for a, b in merged)
    # 重复章数：同区间的多卷里，若含上/中/下分部则为互补内容不算重复；
    # 完全同区间且 part 相同（用户手动重复勾选）才按重复计
    by_iv: dict = {}
    for c in checked:
        if c.start is None or c.end is None:
            continue
        by_iv.setdefault((c.start, c.end), []).append(c.part)
    raw_n = 0
    for (a, b), parts in by_iv.items():
        span = b - a + 1
        raw_n += span if any(p is not None for p in parts) else span * len(parts)
    overlap_n = raw_n - covered_n
    max_chapter = merged[-1][1] if merged else 0
    min_chapter = merged[0][0] if merged else None
    target_max = max(max_chapter, declared_max or 0)
    # 缺失 = 目标范围内未被覆盖的章号
    missing: List[tuple] = []
    lo = min_chapter if min_chapter is not None else (declared_min or 1)
    if merged:
        # 首部（声明起点低于覆盖起点）
        if declared_min and lo > declared_min:
            missing.append((declared_min, lo - 1))
        for (a1, b1), (a2, b2) in zip(merged, merged[1:]):
            if a2 - b1 > 1:
                missing.append((b1 + 1, a2 - 1))
        if target_max > max_chapter:
            missing.append((max_chapter + 1, target_max))
    return {
        "max_chapter": max_chapter, "target_max": target_max,
        "n_covered": len(merged) and sum(1 for c in checked
                                         if c.start is not None) or 0,
        "n_no_range": n_no_range, "covered_n": covered_n,
        "overlap_n": overlap_n, "missing": missing,
        "complete": not missing,
    }


def format_coverage(rep: dict, declared_max: Optional[int] = None) -> str:
    """把 coverage_report 渲染成一行中文说明。"""
    if rep["target_max"] == 0:
        return ("未识别到任何带章号的卷，无法核对完整性"
                + (f"（勾选 {rep['n_no_range']} 卷均为全文/分卷体）"
                   if rep["n_no_range"] else ""))
    head = f"章节完整性核对：最大章号 {rep['max_chapter']}"
    if declared_max and declared_max != rep["max_chapter"]:
        head += f"（标题声明到 {declared_max}）"
    head += (f"；已勾选卷去重覆盖 {rep['covered_n']} 章")
    if rep["overlap_n"]:
        head += f"，多卷重复 {rep['overlap_n']} 章（下载时自动去重）"
    if rep["n_no_range"]:
        head += f"；另有 {rep['n_no_range']} 卷无章号（全文/上中下，不计入）"
    if rep["complete"]:
        head += "。✔ 未发现缺漏章。"
    else:
        miss = "，".join(f"{a}" if a == b else f"{a}-{b}"
                         for a, b in rep["missing"])
        head += f"。⚠ 疑似缺 {miss}（请对照下方卷列表人工确认）。"
    return head


def extract_novel_name(subject: str) -> str:
    """从主帖标题提取小说名。优先取第一对【】内的文字。"""
    s = subject.strip()
    m = re.match(r"\s*【(.+?)】", s)
    if m:
        return m.group(1).strip()
    # 兜底：去掉卷号区间与作者尾巴
    s = re.sub(r"[（(][^（()）]*?\d[^（()）]*?[)）].*$", "", s)
    s = re.sub(r"(作者|著)\s*[:：].*$", "", s)
    return s.strip("　 ") or s


def thread_post_url(tid: str) -> str:
    return f"{SITE}/bbs4/index.php?app=forum&act=threadview&tid={tid}"


_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}


def cn2int(s: str) -> Optional[int]:
    """中文数字/阿拉伯数字 -> int。支持 三十五、一百零五、52 等。"""
    s = s.strip()
    if re.fullmatch(r"[0-9０-９]+", s):
        return int(normalize(s))
    total = num = 0
    seen = False
    for ch in s:
        if ch in _CN_NUM:
            num = _CN_NUM[ch]
            seen = True
        elif ch in _CN_UNIT:
            u = _CN_UNIT[ch]
            total += (num if num else 1) * u
            num, seen = 0, True
        else:
            return None
    return total + num if seen else None


# 章节标题行宽松匹配（作者写法五花八门）。返回 (是否标题行, 章号或None)
_NUMCHAR = r"[0-9０-９一二三四五六七八九十百千万零两〇]{1,10}"
_HDR_RES = [
    # 第四十六章 / 第46章 / 第五十二章 玄关 / 第八卷…第1章（优先取“章”）
    re.compile(rf"^\s*[　]*【?\s*第\s*({_NUMCHAR})\s*[章回卷篇]"),
    # 四十六章 疑点 / 46章（不带“第”，数字与“章”之间不允许空格，行必须短）
    re.compile(rf"^\s*[　]*({_NUMCHAR})章[^。！？，；…]{{0,36}}$"),
    # Chapter 46
    re.compile(r"^\s*[　]*chapter\s*([0-9]{1,4})", re.I),
]
_ANY_DI_RE = re.compile(rf"第\s*({_NUMCHAR})\s*([章回卷篇])")
_HDR_KEYWORD_RE = re.compile(
    r"^\s*[　]*【?\s*(楔子|序章|序幕|引子|前言|后序|后记|尾声|终章|结局|番外)"
    r"[^\n]{0,18}$")


def _chapter_num_in_line(ln: str):
    """一行里可能有 第八卷+第1章，优先取“章”的编号；否则卷/回/篇。"""
    hits = list(_ANY_DI_RE.finditer(ln))
    if not hits:
        return None
    zhang = [cn2int(m.group(1)) for m in hits if m.group(2) == "章"]
    zhang = [z for z in zhang if z is not None]
    if zhang:
        return zhang[-1]
    for m in hits:
        n = cn2int(m.group(1))
        if n is not None:
            return n
    return None


def _match_header(ln: str):
    for p in _HDR_RES:
        m = p.match(ln)
        if m:
            if p is _HDR_RES[0]:
                return True, _chapter_num_in_line(ln)
            n = cn2int(m.group(1))
            if n is not None:
                return True, n
    if _HDR_KEYWORD_RE.match(ln):
        return True, None
    return False, None


def is_chapter_header(line: str):
    """判断整行是否为章节标题行，返回 (bool, 章号int或None)。"""
    ln = line.strip()
    if not ln or len(ln) > 60:
        return False, None
    ok, n = _match_header(ln)
    if ok:
        return True, n
    # “【书名】第二章 xxx” 这类带书名前缀的标题行
    if ln.startswith("【") and "】" in ln:
        tail = ln.split("】", 1)[1].strip()
        if tail:
            return _match_header(tail)
    return False, None


# ---- 文档级宽松分卷样式（严格标题太少时启用，须呈连续递增才采纳） ----
_ALT_STYLES = [
    # （1） / (2) / 【3】 / [4] 独立成行（可带短标题尾巴）
    ("括号数字", re.compile(
        r"^\s*[　]*[（(【\[]\s*(\d{1,4}|[一二三四五六七八九十百千零两〇]{1,8})"
        r"\s*[）)】\]]\s*[^\n]{0,24}$")),
    # 1、 / 2. / 3．开头（阿拉伯数字，可同行接正文）
    ("数字顿号", re.compile(r"^\s*[　]*([0-9]{1,3})\s*[、.．]\s*\S")),
    # 1 / 2 / 3 整行只有数字
    ("裸数字", re.compile(r"^\s*[　]*([0-9]{1,3})\s*$")),
]


def _longest_increasing_run(nums):
    """数字序列中最长的近似连续递增段（相邻差 0~2）。"""
    if not nums:
        return []
    best = cur = [nums[0]]
    for x in nums[1:]:
        if cur and 0 <= x - cur[-1] <= 2:
            cur.append(x)
        else:
            cur = [x]
        if len(cur) > len(best):
            best = cur
    return best


def chapter_markers(text: str):
    """定位章节边界。返回 ([(行号, 章号或None, 行文本)], 采用的样式名)。"""
    lines = text.split("\n")
    prim = []
    for i, l in enumerate(lines):
        ok, n = is_chapter_header(l)
        if ok:
            prim.append((i, n, l.strip()))
    if len(prim) >= 3:
        return prim, "标题"
    best, style = prim, "标题"
    for name, pat in _ALT_STYLES:
        cand = []
        for i, l in enumerate(lines):
            m = pat.match(l)
            if m:
                n = cn2int(m.group(1))
                if n is not None:
                    cand.append((i, n, l.strip()))
        if len(cand) < 3:
            continue
        run = _longest_increasing_run([c[1] for c in cand])
        if len(run) >= 3 and len(run) >= len(cand) * 0.6 and len(cand) > len(best):
            best, style = cand, name
    return best, style


def real_chapter_range(text: str):
    """从卷正文提取真实章号 (min, max, 章数, 样式)；无章节标记返回 None。"""
    markers, style = chapter_markers(text)
    nums = [n for _, n, _ in markers if n is not None]
    if not nums:
        return None
    return min(nums), max(nums), len(nums), style


def parse_thread(html_text: str):
    """返回 dict: subject, date, board, links(list[(tid,text,href)]), body_html"""
    soup = BeautifulSoup(html_text, "html.parser")
    h1 = soup.select_one("h1.main-title") or soup.select_one("h1")
    subject = h1.get_text(strip=True) if h1 else ""
    if not subject and soup.title:
        subject = soup.title.get_text(strip=True)

    date = ""
    sender = soup.select_one(".sender") or soup.select_one(".subtitle-line")
    if sender:
        m = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", sender.get_text(" "))
        if m:
            date = m.group(1)

    board = ""
    m = re.search(r'"sitename":"([^"]+)"', html_text)
    if m:
        board = m.group(1).encode().decode("unicode_escape") if "\\u" in m.group(1) else m.group(1)

    content = soup.select_one("#content-section")
    body_html = ""
    links = []
    if content:
        node = content.find("pre") or content
        for a in content.find_all("a"):
            href = a.get("href", "")
            m = re.search(r"tid=(\d+)", href)
            if m:
                links.append((m.group(1), a.get_text(strip=True),
                              urljoin(SITE + "/bbs4/", href)))
        body_html = str(node)
    return {"subject": subject, "date": date, "board": board,
            "links": links, "body_html": body_html}


def parse_search_page(html_text: str):
    """返回 (items:[(tid,title,date,href)], page_now, page_total)"""
    soup = BeautifulSoup(html_text, "html.parser")
    items = []
    for li in soup.select(".search-content ul li"):
        a = li.find("a")
        if not a:
            continue
        href = a.get("href", "")
        m = re.search(r"tid=(\d+)", href)
        if not m:
            continue
        ps = a.find_all("p")
        title, date = "", ""
        cls = [p.get("class") or [] for p in ps]
        for i, p in enumerate(ps):
            if "lr" in cls[i]:
                date = p.get_text(" ", strip=True)
            elif i >= 1:  # 第0个 p 是板块名
                title = p.get_text("", strip=True)
        items.append({"tid": m.group(1), "title": title, "date": date,
                      "href": urljoin(SITE + "/search.php", href)})
    now = total = 1
    m = re.search(r"第\s*(\d+)\s*/\s*(\d+)\s*页", html_text)
    if m:
        now, total = int(m.group(1)), int(m.group(2))
    return items, now, total


# --------------------------------------------------------------------------- #
# 发现候选卷
# --------------------------------------------------------------------------- #
def search_novel(keyword: str, session, log: Callable[[str], None],
                 max_pages: int = 30):
    out = []
    page = 1
    while page <= max_pages:
        url = (f"{SITE}/search.php?keyword={quote(keyword)}"
               f"&sa={quote(SEARCH_AREA)}" + (f"&p={page}" if page > 1 else ""))
        html_text = fetch(url, session)
        items, now, total = parse_search_page(html_text)
        if not items:
            break
        log(f"搜索第 {now}/{total} 页，{len(items)} 条")
        out.extend(items)
        if now >= total:
            break
        page = now + 1
    return out


def alt_keywords(name: str) -> List[str]:
    """书名缩短的模糊关键字：前6后6（短书名递减），最多2个。"""
    s = (name or "").strip()
    L = len(s)
    kws = []
    if L >= 8:
        kws = [s[:6], s[-6:]]
    elif L >= 5:
        kws = [s[:4], s[-4:]]
    elif L >= 3:
        kws = [s[:2], s[-2:]]
    out = []
    for k in kws:
        k = k.strip("　 【】()（）")
        if k and k != name and k not in out:
            out.append(k)
    return out[:2]


def fuzzy_search(name: str, known_tids: set, session,
                 log: Callable[[str], None],
                 max_pages: int = 3, keep_limit: int = 20) -> List[dict]:
    """用缩短关键字补搜，返回标了 fuzzy 的新条目。"""
    found, seen = [], set(known_tids)
    for kw in alt_keywords(name):
        if kw in (name, ""):
            continue
        log(f"模糊搜索关键字: 「{kw}」")
        try:
            items = search_novel(kw, session, log, max_pages=max_pages)
        except FetchError as e:
            log(f"  失败: {e}")
            continue
        new = [it for it in items if it["tid"] not in seen]
        for it in new[:keep_limit]:
            it["fuzzy"] = True
            found.append(it)
            seen.add(it["tid"])
        log(f"  新增候选 {len(new[:keep_limit])} 条"
            + (f"（超过{keep_limit}条已截断）" if len(new) > keep_limit else ""))
    return found


def _verify_one(c: "Candidate", session, log: Callable[[str], None],
                cancel, label: str = "") -> None:
    """读正文，用实际章节号覆盖该卷区间（发帖标题的卷号经常不准）。"""
    if cancel and cancel.is_set():
        raise FetchError("已取消")
    rng = None
    try:
        info = parse_thread(fetch(c.href or thread_post_url(c.tid), session))
        rng = real_chapter_range(html_to_text(info["body_html"]))
    except Exception as e:  # noqa: BLE001
        log(f"  {label}tid {c.tid} 校验失败({e})，沿用标题卷号")
    if rng:
        rs, re_, n, style = rng
        title_rng = c.range_text()
        tag = f"{n}章" + (f"，按「{style}」识别" if style != "标题" else "")
        if c.start is None or (rs, re_) != (c.start, c.end):
            c.status = (c.status + "; " if c.status else "") + \
                f"标题{title_rng}，实际章节 {rs}-{re_}"
            log(f"  {label}tid {c.tid}: 标题{title_rng} → 实际 {rs}-{re_}"
                f"（{tag}）")
        else:
            log(f"  {label}tid {c.tid}: 实际章节 {rs}-{re_}（{tag}，与标题一致）")
        c.start, c.end = rs, re_
    elif c.start is None:
        log(f"  {label}tid {c.tid}: 未识别到章节标题")


def auto_dedup(cands: List["Candidate"], log: Callable[[str], None],
               verbose: bool = True) -> None:
    """按实际章节区间去重：相同卷号去重 / 完全覆盖排除 / 部分重叠提示。"""
    by_range = {}
    for c in cands:
        if c.start is not None:
            by_range.setdefault((c.start, c.end, c.part), []).append(c)
    for key, group in by_range.items():
        if len(group) < 2:
            continue
        # 保留优先级：非模糊 > 目录收录 > 日期新
        group.sort(key=lambda c: (c.source != "模糊",
                                  "目录" in c.source, c.date or ""),
                   reverse=True)
        keep = group[0]
        for c in group[1:]:
            c.checked = False
            c.status = f"重复卷，已选 tid {keep.tid}"
        if verbose:
            log(f"卷 {keep.range_text()} 有 {len(group)} 帖，保留 tid "
                f"{keep.tid}，排除 {[c.tid for c in group[1:]]}")

    # 规则: 卷号区间重叠
    #   完全包含 -> 取消勾选被包含的一卷
    #   部分重叠 -> 两卷都保留，仅提示（下载时按章节内容哈希自动去重）
    def add_status(c: "Candidate", msg: str):
        if msg not in c.status:
            c.status = (c.status + "; " + msg) if c.status else msg

    active = [c for c in cands if c.checked and c.start is not None]
    pair_done = set()
    for a in active:
        if not a.checked:
            continue
        for b in active:
            if a is b or not b.checked:
                continue
            if a.part != b.part:
                continue   # 同卷号不同上/中/下 = 不同内容，不按覆盖/重叠处理
            if a.start <= b.end and b.start <= a.end:          # 区间重叠
                b_in_a = b.start >= a.start and b.end <= a.end
                a_in_b = a.start >= b.start and a.end <= b.end
                if b_in_a and not a_in_b:
                    b.checked = False
                    add_status(b, f"内容被 {a.range_text()} 覆盖")
                    if verbose:
                        log(f"卷 {b.range_text()} (tid {b.tid}) 被 "
                            f"{a.range_text()} (tid {a.tid}) 覆盖，已取消勾选")
                elif a_in_b and not b_in_a:
                    a.checked = False
                    add_status(a, f"内容被 {b.range_text()} 覆盖")
                    if verbose:
                        log(f"卷 {a.range_text()} (tid {a.tid}) 被 "
                            f"{b.range_text()} (tid {b.tid}) 覆盖，已取消勾选")
                    break
                else:                                          # 部分重叠
                    key = frozenset((a.tid, b.tid))
                    if key in pair_done:
                        continue
                    pair_done.add(key)
                    old, new = ((a, b) if (a.date or "") <= (b.date or "")
                                else (b, a))
                    add_status(old, f"与 {new.range_text()} 章节重叠，"
                                    f"下载时自动按章节去重")
                    if verbose:
                        log(f"卷 {a.range_text()} 与 {b.range_text()} "
                            f"部分重叠，保留两卷，下载时按章节去重")

    if cands and all(not c.checked for c in cands) and \
            any(c.source != "模糊" for c in cands):
        for c in cands:
            if c.source != "模糊":
                c.checked, c.status = True, ""


def analyze(entry_url: str, proxy: str = "", keyword: str = "",
            log: Callable[[str], None] = print,
            cancel: Optional[threading.Event] = None) -> (str, List[Candidate]):
    """
    入口。entry_url 可为小说任一卷的帖子链接（或 search.php 链接/纯书名）。
    返回 (小说名, 候选卷列表[已排序、已标注重复/重叠并预选])。
    """
    session = make_session(proxy)
    log(f"代理: {proxy or '直连'}")

    entry_tid = ""
    m = re.search(r"tid=(\d+)", entry_url or "")
    index_links = {}   # tid -> (title, href)
    subject = ""
    if m and "act=threadview" in entry_url:
        entry_tid = m.group(1)
        log(f"抓取起始帖 tid={entry_tid} ...")
        info = parse_thread(fetch(entry_url, session))
        subject = info["subject"]
        for tid, text, href in info["links"]:
            index_links[tid] = (text, href)
        log(f"主帖标题: {subject}")
        log(f"主帖内目录链接 {len(index_links)} 条")
    elif entry_url and "tid=" not in (entry_url or "") and "http" not in (entry_url or ""):
        keyword = entry_url  # 允许直接给书名

    novel_name = keyword.strip() or extract_novel_name(subject)
    if not novel_name:
        raise FetchError("无法确定书名，请手动填写书名后重试")
    log(f"书名: {novel_name}")

    # 搜索发现
    results = search_novel(novel_name, session, log)
    if cancel and cancel.is_set():
        raise FetchError("已取消")

    # 目录链接里标题包含书名的也并入（防止搜索因分页/跨区遗漏）
    for tid, (text, href) in index_links.items():
        if tid not in {r["tid"] for r in results}:
            results.append({"tid": tid, "title": text, "date": "", "href": href})

    # 过滤 + 去重合并
    want = normalize(novel_name)
    cands: List[Candidate] = []
    seen = set()
    dropped = 0
    for r in results:
        title = r["title"] or (index_links.get(r["tid"], ("", ""))[0])
        if want and want not in normalize(title) and r["tid"] != entry_tid:
            dropped += 1
            continue
        if r["tid"] in seen:
            continue
        seen.add(r["tid"])
        src = []
        if r["tid"] in index_links:
            src.append("目录")
        if r.get("date"):
            src.append("搜索")
        if r["tid"] == entry_tid:
            src.insert(0, "起始帖")
        c = Candidate(tid=r["tid"], title=title, date=r.get("date", ""),
                      href=r["href"] or thread_post_url(r["tid"]),
                      board="", source="+".join(src) or "搜索")
        apply_range_or_part(c)
        cands.append(c)
    if dropped:
        log(f"过滤掉书名不含关键字的结果 {dropped} 条")

    # ---- 模糊补搜：全名一无所获，或标题声明的章数远高于已发现的 ----
    subj_rng = parse_range(subject) if subject else (None, None)
    disc_end = max([c.end for c in cands if c.end] or [0])
    fuzzy_done = False
    need_fuzzy, why = False, ""
    if not cands:
        need_fuzzy, why = True, "全名搜索没有命中任何帖子"
    elif subj_rng[1] and disc_end and subj_rng[1] - disc_end >= 3:
        need_fuzzy, why = True, (f"起始帖标题声明到第{subj_rng[1]}章，"
                                 f"目前只发现到第{disc_end}章")
    if need_fuzzy:
        fuzzy_done = True
        log(f"{why}，启用书名缩短的模糊补搜…")
        for r in fuzzy_search(novel_name, {c.tid for c in cands}, session, log):
            if cancel and cancel.is_set():
                raise FetchError("已取消")
            if r["tid"] in {c.tid for c in cands}:
                continue
            c = Candidate(tid=r["tid"], title=r["title"], date=r.get("date", ""),
                          href=r["href"] or thread_post_url(r["tid"]),
                          source="模糊", checked=False,
                          status="模糊命中，请核对后手动勾选")
            apply_range_or_part(c)
            cands.append(c)
        nf = sum(1 for c in cands if c.source == "模糊")
        if nf:
            log(f"模糊补搜新增 {nf} 条候选（默认未勾选）")
        else:
            log("模糊补搜没有新线索")

    cands.sort(key=lambda c: c.sort_key())

    # ---- 按正文实际章节核准每卷区间（发帖标题的卷号经常不准） ----
    log(f"校验 {len(cands)} 卷的实际章节范围（逐帖读取正文）…")
    for i, c in enumerate(cands, 1):
        _verify_one(c, session, log, cancel, f"[{i}/{len(cands)}] ")
        if i < len(cands):
            time.sleep(0.4)

    cands.sort(key=lambda c: c.sort_key())

    # ---- 自动去重 ----
    auto_dedup(cands, log)

    # 标题里出现过的最大/最小章号（作为“全书应有范围”的声明基准）
    title_ends = [parse_range(c.title)[1] for c in cands]
    title_ends = [e for e in title_ends if e]
    title_starts = [parse_range(c.title)[0] for c in cands]
    title_starts = [s for s in title_starts if s]
    declared_max = max(title_ends + ([subj_rng[1]] if subj_rng[1] else [])) \
        if (title_ends or subj_rng[1]) else None
    declared_min = (min(title_starts + ([subj_rng[0]] if subj_rng[0] else []))
                    if (title_starts or subj_rng[0]) else None)
    # ---- 校验后完整性修补：覆盖对账发现缺章 → 缩短关键字再补搜一轮 ----
    if not fuzzy_done and any(c.source != "模糊" for c in cands):
        rep = coverage_report(cands, declared_min, declared_max)
        if rep["missing"]:
            miss = "、".join(f"{a}" if a == b else f"{a}-{b}"
                             for a, b in rep["missing"])
            log(f"完整性核对发现疑似缺章 {miss}，用缩短关键字补搜缺口…")
            fresh = []
            for r in fuzzy_search(novel_name, {c.tid for c in cands},
                                  session, log):
                if cancel and cancel.is_set():
                    raise FetchError("已取消")
                if r["tid"] in {c.tid for c in cands}:
                    continue
                c = Candidate(tid=r["tid"], title=r["title"],
                              date=r.get("date", ""),
                              href=r["href"] or thread_post_url(r["tid"]),
                              source="模糊", checked=False,
                              status="模糊命中，请核对后手动勾选")
                apply_range_or_part(c)
                if c.start is not None and any(a <= c.end and c.start <= b
                                               for a, b in rep["missing"]):
                    # 章号正好压住缺口 → 大概率就是缺的那卷，自动勾选
                    c.checked = True
                    c.status = f"模糊命中：填补 {c.range_text()} 缺口，请留意"
                cands.append(c)
                fresh.append(c)
            if fresh:
                log(f"缺口补搜新增 {len(fresh)} 条候选，逐帖校验…")
                for c in fresh:
                    _verify_one(c, session, log, cancel, "[补搜] ")
                    time.sleep(0.4)
                cands.sort(key=lambda c: c.sort_key())
                auto_dedup(cands, log, verbose=False)
            else:
                log("缺口补搜没有新线索（缺失章可能未发布、被删或标题差异过大）")

    meta = {"declared_min": declared_min, "declared_max": declared_max}
    return novel_name, cands, meta


# --------------------------------------------------------------------------- #
# 正文提取
# --------------------------------------------------------------------------- #
def html_to_text(body_html: str, drop_links: bool = True) -> str:
    soup = BeautifulSoup(body_html, "html.parser")
    root = soup.find("pre") or soup
    for a in root.find_all("a"):
        if drop_links:
            a.decompose()
        else:
            a.replace_with(a.get_text())
    for t in root.find_all("script"):
        t.decompose()
    for br in root.find_all("br"):
        br.replace_with("\n")
    for p in root.find_all("p"):
        p.replace_with("\n")
    txt = root.get_text()
    lines = [ln.rstrip() for ln in txt.splitlines()]
    out, blank = [], False
    for ln in lines:
        if not ln.strip():
            if not blank and out:
                out.append("")
            blank = True
        else:
            out.append(ln)
            blank = False
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


sanitize_re = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def split_chapters(text: str):
    """按章节边界切分为 [(header, body)]；首章前的文字 header 为 ''。"""
    markers, _ = chapter_markers(text)
    lines = text.split("\n")
    cut = {i: h for i, _n, h in markers}
    parts, hdr, buf = [], "", []
    for i, ln in enumerate(lines):
        if i in cut:
            parts.append((hdr, "\n".join(buf)))
            hdr, buf = cut[i], []
        else:
            buf.append(ln)
    parts.append((hdr, "\n".join(buf)))
    return parts


def dedup_text(text: str, seen: set, log, tag: str) -> str:
    """按章节正文内容哈希跨卷去重（重复转载的章节跳过）。返回保留文本。"""
    import hashlib
    parts = split_chapters(text)
    markers = sum(1 for h, _ in parts if h)
    kept = []
    for hdr, body in parts:
        core = normalize(body)
        if len(core) > 20:
            key = hashlib.sha1(core.encode("utf-8")).hexdigest()
        elif hdr:
            key = "H:" + normalize(hdr)
        else:
            key = None
        if key and key in seen:
            log(f"  跳过重复章节: {hdr or '(卷首文字)'} [{tag}]")
            continue
        if key:
            seen.add(key)
        kept.append((hdr + "\n" + body) if hdr else body)
    return "\n".join(kept).strip("\n")


def safe_filename(name: str, limit: int = 80) -> str:
    name = sanitize_re.sub("", name).strip(" .　")
    return (name[:limit] or "novel") + ".txt"


def download_novel(novel_name: str, cands: List[Candidate], out_dir: str,
                   proxy: str = "", interval: float = 0.8,
                   log: Callable[[str], None] = print,
                   cancel: Optional[threading.Event] = None,
                   progress: Optional[Callable[[int, int], None]] = None,
                   preserve_order: bool = False) -> str:
    if preserve_order:      # 按界面当前行序（用户可能手动拖动调整过）
        picked = [c for c in cands if c.checked]
    else:
        picked = sorted([c for c in cands if c.checked],
                        key=lambda c: c.sort_key())
    if not picked:
        raise FetchError("没有勾选任何卷")
    session = make_session(proxy)
    parts = []
    seen_chapters = set()
    n = len(picked)
    for i, c in enumerate(picked, 1):
        if cancel and cancel.is_set():
            raise FetchError("已取消")
        log(f"[{i}/{n}] 下载 tid={c.tid} {c.range_text()} ...")
        try:
            info = parse_thread(fetch(c.href or thread_post_url(c.tid), session))
        except NotFoundError:
            log(f"  帖子已被删除(404)，跳过 tid={c.tid}")
            continue
        text = html_to_text(info["body_html"])
        text = dedup_text(text, seen_chapters, log, f"tid {c.tid}")
        if not text.strip():
            log(f"  警告: tid={c.tid} 正文为空或全部重复，跳过")
            continue
        head = f"\n{'=' * 30}\n{c.title or (novel_name + c.range_text())}\n"
        if c.date:
            head += f"发帖时间: {c.date}    tid: {c.tid}\n"
        head += "=" * 30 + "\n"
        parts.append(head + "\n" + text + "\n")
        log(f"  {len(text)} 字")
        if progress:
            progress(i, n)
        if i < n:
            time.sleep(interval)

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, safe_filename(novel_name))
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{novel_name}\n")
        f.write("来源: " + ", ".join(c.href for c in picked) + "\n")
        f.write(f"共 {len(parts)} 卷\n")
        f.write("\n".join(parts))
    total = sum(len(p) for p in parts)
    log(f"完成: {path}  （正文约 {total} 字）")
    return path


# --------------------------------------------------------------------------- #
# 命令行入口（调试/测试用）
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["analyze", "download"])
    ap.add_argument("entry")
    ap.add_argument("--keyword", default="")
    ap.add_argument("--proxy", default=DEFAULT_PROXY)
    ap.add_argument("--out", default=".")
    ap.add_argument("--interval", type=float, default=0.8)
    ap.add_argument("--tids", default="", help="逗号分隔，仅下载这些tid")
    a = ap.parse_args()

    name, cands, meta = analyze(a.entry, a.proxy, a.keyword)
    print("=" * 70)
    print(f"书名: {name}")
    for c in cands:
        print(f"  [{'x' if c.checked else ' '}] {c.tid:>9} "
              f"{c.range_text():<10} {c.date:<17} {c.source:<9} "
              f"{c.title[:44]}  {c.status}")
    print(format_coverage(coverage_report(
        cands, meta.get("declared_min"), meta.get("declared_max")),
        meta.get("declared_max")))
    if a.cmd == "download":
        if a.tids:
            tids = set(a.tids.split(","))
            for c in cands:
                c.checked = c.tid in tids
                c.status = ""
        p = download_novel(name, cands, a.out, a.proxy, a.interval)
        print(p)


if __name__ == "__main__":
    main()
