# -*- coding: utf-8 -*-
"""
搜索引擎模块：Google Lens + Yandex 以图搜图

流程说明（均为网页版逆向接口，无需 API Key）：
  Google Lens:  POST https://lens.google.com/v3/upload  (multipart: encoded_image)
                -> 重定向到结果页 -> 解析 HTML 中的来源链接
  Yandex:       POST https://yandex.ru/images/search    (multipart: upfile, format=json)
                -> 返回 cbirId -> GET 结果页 -> 解析页面内嵌 JSON
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from html import unescape
from typing import List, Optional
from urllib.parse import quote, urlparse

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

GOOGLE = "Google"
YANDEX = "Yandex"

TIMEOUT = 40


@dataclass
class SearchResult:
    engine: str          # "Google" / "Yandex"
    title: str           # 页面标题
    url: str             # 来源页面链接
    domain: str          # 来源域名
    thumb: str = ""      # 缩略图 URL（可为空）
    origin: str = ""     # 高清原图 URL（可为空，Yandex 提供）
    frame: int = 0       # 视频帧序号（图片搜索时为 0）


@dataclass
class EngineOutcome:
    engine: str
    page_url: str = ""                 # 引擎结果页地址（可在浏览器打开）
    results: List[SearchResult] = field(default_factory=list)
    error: str = ""                    # 出错信息（可为空）


def _new_session(proxy: Optional[str] = None) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def _domain_of(url: str) -> str:
    try:
        d = urlparse(url).netloc.lower()
        if d.startswith("www."):
            d = d[4:]
        return d
    except Exception:
        return ""


def _fix_thumb(u: str) -> str:
    if u.startswith("//"):
        return "https:" + u
    return u


# ============================ Google Lens ============================

_LENS_BAD_HOSTS = (
    "google.", "gstatic.", "googleapis.", "googlesyndication.",
    "doubleclick.", "gstatic.com", "schema.org", "w3.org",
    "youtube.com", "blogger.com", "googleusercontent.",
)


def google_lens_search(img_bytes: bytes, proxy: Optional[str] = None,
                       frame: int = 0, log=print) -> EngineOutcome:
    """上传图片到 Google Lens 并解析结果页"""
    oc = EngineOutcome(engine=GOOGLE)
    try:
        s = _new_session(proxy)
        # 跳过欧洲节点的 GDPR 同意页
        s.cookies.set("CONSENT", "YES+cb.20220419-08-p0.cs+FX+111",
                      domain=".google.com")
        log("Google Lens: 正在上传图片…")
        resp = s.post(
            "https://lens.google.com/v3/upload",
            params={"re": "df", "stcs": str(int(time.time() * 1000))},
            files={"encoded_image": ("image.jpg", img_bytes, "image/jpeg")},
            data={"processed_image_dimensions": "800,600"},
            headers={"Referer": "https://lens.google.com/",
                     "Origin": "https://lens.google.com"},
            timeout=TIMEOUT,
        )
        oc.page_url = resp.url
        if "consent.google" in resp.url:
            oc.error = "被 Google 同意页拦截，请在浏览器完成一次同意或更换代理节点"
            return oc
        if resp.status_code != 200:
            oc.error = f"上传失败，HTTP {resp.status_code}"
            return oc
        oc.results = _parse_lens(resp.text, frame)
        log(f"Google Lens: 解析到 {len(oc.results)} 条结果")
        if not oc.results:
            oc.error = "已获取结果页，但未自动解析出条目（可点「打开结果页」在浏览器查看）"
    except requests.exceptions.ProxyError:
        oc.error = "代理连接失败，请检查代理地址 / 端口"
    except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError):
        oc.error = "连接 Google 失败（国内网络需在左侧填写代理，如 http://127.0.0.1:7890）"
    except Exception as e:  # noqa: BLE001
        oc.error = f"{type(e).__name__}: {e}"
    return oc


def _parse_lens(html: str, frame: int) -> List[SearchResult]:
    """尽力解析 Google Lens 结果页中的外部来源链接（视觉匹配/页面匹配）"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    items, seen = [], set()
    for a in soup.select("a[href^='http']"):
        href = a.get("href", "")
        host = _domain_of(href)
        if not host or any(b in host for b in _LENS_BAD_HOSTS):
            continue
        img = a.find("img")
        title = a.get_text(" ", strip=True)
        if not title and img is not None:
            title = img.get("alt", "")
        title = re.sub(r"\s+", " ", title).strip()
        if len(title) < 2 or len(title) > 300:
            continue
        thumb = ""
        if img is not None:
            thumb = _fix_thumb(img.get("src") or img.get("data-src") or "")
        key = (host, title[:80])
        if key in seen:
            continue
        seen.add(key)
        items.append(SearchResult(GOOGLE, title[:200], href, host, thumb, frame))
        if len(items) >= 60:
            break
    return items


# ============================ Yandex ============================

def yandex_search(img_bytes: bytes, proxy: Optional[str] = None,
                  frame: int = 0, log=print) -> EngineOutcome:
    """上传图片到 Yandex 图片搜索并解析结果"""
    oc = EngineOutcome(engine=YANDEX)
    try:
        s = _new_session(proxy)
        log("Yandex: 正在上传图片…")
        try:
            # 先拿 cookie，降低触发风控的概率
            s.get("https://yandex.ru/images/", timeout=15)
        except Exception:  # noqa: BLE001
            pass
        resp = s.post(
            "https://yandex.ru/images/search",
            params={
                "from": "tabbar",
                "rpt": "imageview",
                "format": "json",
                "request": '{"blocks":[{"block":"b-page_type_search-by-image__link"}]}',
            },
            files={"upfile": ("blob", img_bytes, "image/jpeg")},
            headers={"Referer": "https://yandex.ru/images/",
                     "Origin": "https://yandex.ru"},
            timeout=TIMEOUT,
        )
        txt = resp.text.strip()
        if not txt.startswith("{"):
            oc.page_url = "https://yandex.ru/images/"
            oc.error = "Yandex 触发了人机验证（IP 风控），请稍后重试或更换网络 / 代理"
            return oc
        params = json.loads(txt)["blocks"][0]["params"]
        cbir_id = params.get("cbirId", "")
        if not cbir_id:
            oc.error = "Yandex 未返回 cbirId，接口可能已变更"
            return oc
        page = ("https://yandex.ru/images/search?rpt=imageview&cbir_id="
                + quote(cbir_id, safe=""))
        oc.page_url = page
        log("Yandex: 正在抓取结果页…")
        html = s.get(page, timeout=TIMEOUT).text
        oc.results = _parse_yandex(html, frame)
        log(f"Yandex: 解析到 {len(oc.results)} 条结果")
        if not oc.results:
            oc.error = "结果页已获取但未解析到条目（可点「打开结果页」在浏览器查看）"
    except requests.exceptions.ProxyError:
        oc.error = "代理连接失败，请检查代理地址 / 端口"
    except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError):
        oc.error = "连接 Yandex 失败，请检查网络"
    except Exception as e:  # noqa: BLE001
        oc.error = f"{type(e).__name__}: {e}"
    return oc


def _parse_yandex(html: str, frame: int) -> List[SearchResult]:
    """解析 Yandex 结果页内嵌的 JSON（title/description/url/domain/thumb）"""
    text = unescape(html)
    dec = json.JSONDecoder()
    items, seen = [], set()
    for m in re.finditer(r'\{"title":"', text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(obj, dict):
            continue
        url = obj.get("url", "")
        domain = obj.get("domain", "")
        if not (isinstance(url, str) and url.startswith("http") and domain):
            continue
        if url in seen:
            continue
        seen.add(url)
        thumb = obj.get("thumb", {})
        thumb_url = _fix_thumb(thumb.get("url", "")) if isinstance(thumb, dict) else ""
        orig = obj.get("originalImage", {})
        origin_url = _fix_thumb(orig.get("url", "")) if isinstance(orig, dict) else ""
        title = re.sub(r"\s+", " ", str(obj.get("title", ""))).strip()
        items.append(SearchResult(YANDEX, title[:200] or domain, url,
                                  str(domain), thumb_url, origin_url, frame))
        if len(items) >= 80:
            break
    return items


# ============================ 交叉汇总 ============================

def aggregate(results: List[SearchResult]) -> List[dict]:
    """按域名交叉汇总：同一域名被越多的引擎 / 帧命中，排名越靠前"""
    groups = {}
    for r in results:
        g = groups.get(r.domain)
        if g is None:
            g = groups[r.domain] = {
                "domain": r.domain, "engines": set(), "frames": set(),
                "title": r.title, "url": r.url, "thumb": r.thumb,
                "origin": r.origin, "count": 0,
            }
        g["engines"].add(r.engine)
        g["frames"].add(r.frame)
        g["count"] += 1
        if (not g["title"] or g["title"] == g["domain"]) and r.title:
            g["title"], g["url"] = r.title, r.url
        if not g["thumb"] and r.thumb:
            g["thumb"] = r.thumb
        if not g["origin"] and r.origin:
            g["origin"] = r.origin
    return sorted(groups.values(),
                  key=lambda g: (-(len(g["engines"]) * 10 + len(g["frames"])),
                                 -g["count"], g["domain"]))
