#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.summarizer
========================
离线开发者动态摘要 —— 无需外部 LLM API。

策略：
1. 关键词主题分类（量子、闪电、隐私、共识、钱包、L2 等）
2. 高频关键词提取（标题 + 摘要 tokenize）
3. 跨源信号检测（同一主题出现在 ≥2 个源 → 重要议题）
4. 输出结构化模板供前端渲染

设计目标：
- 0 成本、0 延迟、稳定可预测
- 后期若想接 LLM，只需在 fetch_builders_feed 里多调一个函数即可
"""

import re
from collections import Counter
from datetime import datetime
from typing import List, Dict


# ──────────────────────────────────────────────────────────────
# 主题分类规则（按 Bitcoin 开发者社区重要性排序）
# 每个 topic: (display_name, icon, [关键词列表，大小写不敏感])
# ──────────────────────────────────────────────────────────────
TOPICS = [
    ("抗量子安全", "🔮", [
        "quantum", "post-quantum", "post quantum", "p2wots", "bip324", "bip-324",
        "lattice", "falcon", "dilithium", "hash-based signature", "ml-dsa",
        "qrl", "qubit", "shor", "grover",
    ]),
    ("闪电网络", "⚡", [
        "lightning", "lightning network", "core lightning", " cln ", "ldk",
        "lnd", "eclair", "bolt", "bolt12", "htlc", "splice", "channel",
        "lsp ", "gossip", "wumbo", "submarine swap",
    ]),
    ("共识/软分叉", "🔧", [
        "consensus", "soft fork", "soft-fork", "hard fork", "activation",
        "covenant", "ctv", "op_cat", "op_checktemplate", "checktemplateverify",
        "anyprevout", " apo ", "lnhance", "drivechain", "bip300", "bip301",
        "miniscript",
    ]),
    ("隐私", "🕶️", [
        "privacy", "coinjoin", "silent payment", "bip352", "p2mr",
        "payjoin", "mempool privacy", "stealth", "anonymity", "confidential",
    ]),
    ("Taproot/Schnorr", "🌳", [
        "taproot", "schnorr", "musig", "musig2", "frost", "tapscript",
        "ptlc", "adaptor signature",
    ]),
    ("钱包/签名设备", "👛", [
        "wallet", "hardware wallet", " jade ", "coldcard", "trezor", "ledger",
        "seedsigner", "passport", "blockstream jade", "descriptor",
        "miniscript wallet", "seed phrase",
    ]),
    ("Mempool/节点", "📡", [
        "mempool", "bitcoin core", "libbitcoinkernel", "full node",
        "package relay", " rbf ", "replace-by-fee", "cluster mempool",
        "p2p", "stratum", "compact block",
    ]),
    ("L2/侧链", "🪐", [
        " l2 ", "sidechain", " ark ", "spiderchain", "drivechain", "liquid",
        "statechain", "federation", "bitvm", "rollup", "fedimint",
    ]),
    ("挖矿", "⛏️", [
        "mining", "hashrate", "miner", "mining pool", "stratum v2",
        " asic ", "block template", "datum",
    ]),
    ("BIP/标准", "📜", [
        " bip ", "bip-", "bip335", "bip352", "bip324", "rfc ", "proposal",
        "specification",
    ]),
    ("Ordinals/铭文", "🖼️", [
        "ordinal", "inscription", "rune", "brc-20", "brc20", "atomical",
    ]),
    ("安全/漏洞", "🛡️", [
        "vulnerability", " cve", "exploit", "security audit", "advisory",
        "disclosure", "denial of service", " dos ",
    ]),
]

# 与 RSS source key 的映射（用于跨源统计）
SOURCE_SHORT = {
    "optech":     "Optech",
    "delving":    "Delving",
    "devmail":    "Dev List",
    "blockstream":"Blockstream",
}

# 每个主题的一句话中文背景描述（用于生成 takeaway）
TOPIC_CONTEXT = {
    "抗量子安全":     "围绕 BTC 抗量子签名方案与 BIP324 加密传输",
    "闪电网络":      "Lightning 协议演进、通道与流动性优化",
    "共识/软分叉":    "共识层升级提案与软分叉激活路径",
    "隐私":          "链上隐私增强与混币/隐形支付方案",
    "Taproot/Schnorr": "Taproot、Schnorr 多签与 MuSig 应用",
    "钱包/签名设备":   "硬件钱包与签名器形态、Miniscript 落地",
    "Mempool/节点":   "Bitcoin Core、内存池策略与节点 P2P 层",
    "L2/侧链":       "L2、侧链、BitVM 与 Federation 形态实验",
    "挖矿":          "矿池协议（Stratum V2）、算力与区块模板",
    "BIP/标准":      "新 BIP 提案与协议标准化进展",
    "Ordinals/铭文":  "Ordinals、Runes、铭文协议生态",
    "安全/漏洞":      "披露的安全公告、CVE 与攻击面分析",
}

# tokenize 用停用词（节略，仅过滤掉最常见的 + 源特定噪音）
STOPWORDS = set("""
a an the and or but of for to in on with by from as is are was were be been being
this that these those there here it its his her their we you i my your our they them
will would could should can may might must do does did has have had not no nor so if
then than because while when where what who which how why all any both each every
new use using used see also more most some such per via re fw fwd
""".split() + [
    # 源特定品牌/作者（出现在每篇标题里，无信息量）
    "bitcoin", "newsletter", "optech", "delving", "blockstream", "bitcoindev",
    "murch", "gnusha", "issue", "podcast", "weekly", "week", "update", "post",
    "summary", "recap", "discussion", "thread",
    # 邮件附件头噪音
    "attachment", "bytes", "plain", "text", "type", "size", "draft", "html",
    # podcast 嘉宾名 / 通用动词（噪音）
    "mark", "mike", "erhardt", "schmidt", "gustavo", "flores", "echaiz",
    "joined", "discuss", "wrote", "describes", "talks", "says", "thanks",
    "hello", "hello,", "hey", "hi,", "feedback", "regards", "best",
])

# 高频词的最小长度
_MIN_WORD_LEN = 4


def _normalize(text: str) -> str:
    """全小写 + 多空格归一，保留连字符以匹配 BIP-324 等"""
    if not text:
        return ""
    t = text.lower()
    # 标点 → 空格（保留 -）
    t = re.sub(r"[^\w\s\-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return f" {t} "  # 前后加空格，让 " bip " 这种模糊匹配生效


def _classify_item(item: dict) -> List[str]:
    """返回该 item 命中的主题列表（可能多个）"""
    text = _normalize(f"{item.get('title','')} {item.get('summary','')}")
    matched = []
    for topic_name, _icon, keywords in TOPICS:
        for kw in keywords:
            if kw.lower() in text:
                matched.append(topic_name)
                break
    return matched


def _extract_keywords(items: List[dict], top_n: int = 12) -> List[Dict]:
    """从所有 items 标题+摘要里提取高频关键词"""
    counter = Counter()
    for item in items:
        text = _normalize(f"{item.get('title','')} {item.get('summary','')}")
        # 切词
        words = re.findall(r"[a-z][a-z0-9\-]+", text)
        for w in words:
            if len(w) < _MIN_WORD_LEN:
                continue
            if w in STOPWORDS:
                continue
            # 跳过纯数字-连字符
            if w.replace("-", "").isdigit():
                continue
            counter[w] += 1
    return [
        {"word": w, "count": c}
        for w, c in counter.most_common(top_n)
        if c >= 2  # 至少出现 2 次才算高频
    ]


def summarize_builders_feed(feed_data: dict) -> dict:
    """
    对 fetch_builders_feed 的返回做主题聚合摘要。
    输入: {"sources": [{"key", "name", "icon", "items": [...]}, ...], "total": int}
    输出: summary dict（嵌入 feed_data["summary"] 里供前端渲染）
    """
    if not feed_data or not feed_data.get("sources"):
        return {
            "generated_at": datetime.now().strftime("%H:%M"),
            "highlights": [],
            "trending_keywords": [],
            "cross_source_topics": [],
            "total_items": 0,
            "note": "无可用数据",
        }

    # 1) 摊平所有条目，附带源信息
    all_items = []
    for src in feed_data["sources"]:
        src_key = src.get("key", "")
        src_name = src.get("name", "")
        src_icon = src.get("icon", "")
        for it in src.get("items", []):
            all_items.append({
                **it,
                "_src_key": src_key,
                "_src_name": src_name,
                "_src_icon": src_icon,
            })

    if not all_items:
        return {
            "generated_at": datetime.now().strftime("%H:%M"),
            "highlights": [],
            "trending_keywords": [],
            "cross_source_topics": [],
            "total_items": 0,
            "note": "暂无条目",
        }

    # 2) 主题分类：item → [topics]，同时记录每个 topic 命中的 items
    topic_to_items: Dict[str, List[dict]] = {}
    topic_to_sources: Dict[str, set] = {}
    for it in all_items:
        topics = _classify_item(it)
        for t in topics:
            topic_to_items.setdefault(t, []).append(it)
            topic_to_sources.setdefault(t, set()).add(it["_src_key"])

    # 3) Highlights：按命中条数降序，取前 5 个主题，每主题展示前 3 条
    topic_meta = {name: (icon, kws) for name, icon, kws in TOPICS}
    sorted_topics = sorted(
        topic_to_items.items(),
        key=lambda kv: (-len(kv[1]), -len(topic_to_sources.get(kv[0], set())))
    )
    highlights = []
    for topic, items in sorted_topics[:5]:
        icon, _ = topic_meta.get(topic, ("•", []))
        # 取前 3 条，优先按日期倒序
        items_sorted = sorted(items, key=lambda x: x.get("date", ""), reverse=True)[:3]
        src_count = len(topic_to_sources[topic])
        ctx = TOPIC_CONTEXT.get(topic, "")
        # 中文一句话要点
        if src_count >= 3:
            takeaway = f"全社区焦点（{src_count} 源 / {len(items)} 条）：{ctx}。"
        elif src_count == 2:
            takeaway = f"双源讨论（{len(items)} 条）：{ctx}。"
        else:
            takeaway = f"单源专题（{len(items)} 条）：{ctx}。"
        highlights.append({
            "topic": topic,
            "icon": icon,
            "count": len(items),
            "sources": sorted(SOURCE_SHORT.get(s, s) for s in topic_to_sources[topic]),
            "takeaway": takeaway,
            "items": [
                {
                    "title": it["title"],
                    "url": it["url"],
                    "date": it.get("date", ""),
                    "source": it["_src_name"],
                    "source_icon": it["_src_icon"],
                }
                for it in items_sorted
            ],
        })

    # 4) 跨源信号：同一主题出现在 ≥2 个源 → 全社区热议
    cross_source = []
    for topic, sources in topic_to_sources.items():
        if len(sources) >= 2:
            icon, _ = topic_meta.get(topic, ("•", []))
            cross_source.append({
                "topic": topic,
                "icon": icon,
                "sources": sorted(SOURCE_SHORT.get(s, s) for s in sources),
                "source_count": len(sources),
                "item_count": len(topic_to_items[topic]),
            })
    cross_source.sort(key=lambda x: (-x["source_count"], -x["item_count"]))

    # 5) 高频关键词
    trending = _extract_keywords(all_items, top_n=15)

    # 6) 中文叙述段落 narrative —— 把高维数据揉成 2-3 句人话
    narrative = _build_narrative(
        total_items=len(all_items),
        total_sources=len(feed_data["sources"]),
        highlights=highlights,
        cross_source=cross_source,
        trending=trending,
    )

    return {
        "generated_at": datetime.now().strftime("%H:%M"),
        "total_items": len(all_items),
        "total_sources": len(feed_data["sources"]),
        "narrative": narrative,
        "highlights": highlights,
        "cross_source_topics": cross_source[:6],
        "trending_keywords": trending,
        "method": "离线模板聚合（关键词分类 + 跨源信号 + 高频词）",
    }


def _build_narrative(total_items, total_sources, highlights, cross_source, trending):
    """
    生成 2-3 句中文叙述。模板：
    『本期共收录 N 条开发动态（K 个源）。最受关注的是【X 议题】（M 条/N 源），其次是【Y 议题】。
     全社区聚焦于 [跨源议题列表]。高频词为 [前 5 个英文术语]。』
    """
    if total_items == 0:
        return "暂无开发动态可总结。"

    parts = []
    parts.append(f"本期共收录 **{total_items} 条** Bitcoin 开发动态，覆盖 **{total_sources} 个源**。")

    # 描述前 2 个最热主题
    if len(highlights) >= 1:
        h0 = highlights[0]
        s0 = f"出现在 {len(h0['sources'])} 个源" if len(h0['sources']) >= 2 else "聚焦在单个源"
        line = f"最受关注的是 **{h0['icon']} {h0['topic']}**（{h0['count']} 条 · {s0}）"
        if len(highlights) >= 2:
            h1 = highlights[1]
            line += f"，其次是 **{h1['icon']} {h1['topic']}**（{h1['count']} 条）"
        line += "。"
        parts.append(line)

    # 跨源信号
    multi = [t for t in cross_source if t["source_count"] >= 3]
    if multi:
        topics_str = "、".join(f"{t['icon']} {t['topic']}" for t in multi[:3])
        parts.append(f"全社区共同热议：{topics_str}（≥3 个源同时讨论）。")
    elif cross_source:
        topics_str = "、".join(f"{t['icon']} {t['topic']}" for t in cross_source[:2])
        parts.append(f"双源讨论的议题包括：{topics_str}。")

    # 高频技术术语
    if trending:
        top_terms = ", ".join(f"`{k['word']}`" for k in trending[:5])
        parts.append(f"高频技术术语：{top_terms}。")

    return " ".join(parts)
