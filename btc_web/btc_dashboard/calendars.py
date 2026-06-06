#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btc_dashboard.calendars
=======================
加密项目日历 + 宏观经济日历。
"""

import requests
from datetime import datetime, timedelta, timezone
from typing import Tuple, Dict, Optional


def fetch_crypto_calendar() -> list:
    """
    获取加密货币日历 - 从律动 BlockBeats 获取
    - 代币解锁、空投、上线等事件
    - 使用关键词筛选相关快讯
    """
    crypto_events = []
    
    # 事件关键词分类
    event_keywords = {
        "解锁": ("🔓", "代币解锁", "高"),
        "空投": ("🪂", "空投", "高"),
        "上线": ("🚀", "上线", "中"),
        "升级": ("⚡", "升级", "中"),
        "主网": ("🌐", "主网", "中"),
        "测试网": ("🧪", "测试网", "低"),
        "发布": ("📢", "发布", "中"),
        "Unlock": ("🔓", "代币解锁", "高"),
        "Airdrop": ("🪂", "空投", "高"),
        "Launch": ("🚀", "上线", "中"),
    }
    
    try:
        # 从 BlockBeats Flash API 获取快讯
        response = requests.get(
            "https://api.theblockbeats.news/v1/open-api/open-flash",
            params={"size": 50, "page": 1, "type": "push", "lang": "cn"},
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        )
        
        if response.status_code == 200:
            data = response.json()
            items = data.get("data", {}).get("data", [])
            
            for item in items:
                title = item.get("title", "")
                content = item.get("content", "")
                full_text = title + content
                
                # 检查是否包含事件关键词
                for keyword, (icon, event_type, impact) in event_keywords.items():
                    if keyword in full_text:
                        # 提取时间信息
                        add_time = item.get("add_time", 0)
                        if add_time:
                            event_time = datetime.fromtimestamp(add_time)
                            time_str = event_time.strftime("%m-%d %H:%M")
                        else:
                            time_str = "即时"
                        
                        # 截取标题
                        display_title = title[:40] + "..." if len(title) > 40 else title
                        
                        crypto_events.append({
                            "event": display_title,
                            "date": time_str,
                            "status": event_type,
                            "impact": impact,
                            "type": "加密事件",
                            "icon": icon,
                            "url": f"https://www.theblockbeats.info/flash/{item.get('id', '')}"
                        })
                        break  # 只匹配第一个关键词
                
                if len(crypto_events) >= 8:
                    break
                    
    except Exception as e:
        print(f"⚠️ BlockBeats Calendar API 失败: {e}")
    
    # 如果没有获取到事件，添加一个提示
    if not crypto_events:
        crypto_events.append({
            "event": "暂无即时事件",
            "date": "",
            "status": "查看律动日历",
            "impact": "",
            "type": "提示",
            "icon": "📅",
            "url": "https://www.theblockbeats.info/calendar"
        })
    
    # 添加律动日历链接
    crypto_events.append({
        "event": "🔗 更多加密日历",
        "date": "",
        "status": "查看全部",
        "impact": "",
        "type": "链接",
        "icon": "🔗",
        "url": "https://www.theblockbeats.info/calendar"
    })
    
    return crypto_events


def fetch_macro_calendar() -> list:
    """
    获取宏观经济日历
    - 使用 faireconomy.media API (基于 Forex Factory)
    - 筛选美元相关的高影响事件：CPI、NFP、FOMC等
    - 中文翻译 + 实际/预期值显示
    """
    calendar = []
    
    # 英文 -> 中文名称映射
    name_translations = {
        # 通胀数据
        'CPI m/m': '📊 CPI 月率',
        'Core CPI m/m': '📊 核心CPI 月率',
        'CPI y/y': '📊 CPI 年率',
        'Core CPI y/y': '📊 核心CPI 年率',
        'PPI m/m': '📊 PPI 月率',
        'Core PPI m/m': '📊 核心PPI 月率',
        'PCE Price Index m/m': '📊 PCE物价指数 月率',
        'Core PCE Price Index m/m': '📊 核心PCE物价指数 月率',
        # 就业数据
        'Non-Farm Employment Change': '👷 非农就业人数',
        'Unemployment Rate': '👷 失业率',
        'Unemployment Claims': '👷 初请失业金人数',
        'Average Hourly Earnings m/m': '👷 平均时薪 月率',
        'Employment Cost Index q/q': '👷 就业成本指数 季率',
        'ADP Non-Farm Employment Change': '👷 ADP非农就业人数',
        'JOLTS Job Openings': '👷 职位空缺数',
        # 利率/美联储
        'Federal Funds Rate': '🏦 联邦基金利率',
        'FOMC Statement': '🏦 FOMC声明',
        'FOMC Meeting Minutes': '🏦 FOMC会议纪要',
        'Fed Chair Powell Speaks': '🏦 鲍威尔讲话',
        # GDP/经济增长
        'Advance GDP q/q': '📈 GDP初值 季率',
        'Prelim GDP q/q': '📈 GDP修正值 季率',
        'Final GDP q/q': '📈 GDP终值 季率',
        # 零售/消费
        'Retail Sales m/m': '🛒 零售销售 月率',
        'Core Retail Sales m/m': '🛒 核心零售销售 月率',
        'Consumer Confidence': '🛒 消费者信心指数',
        'CB Consumer Confidence': '🛒 谘商会消费者信心指数',
        # 制造业/服务业
        'ISM Manufacturing PMI': '🏭 ISM制造业PMI',
        'ISM Services PMI': '🏭 ISM服务业PMI',
        'Durable Goods Orders m/m': '🏭 耐用品订单 月率',
        'Core Durable Goods Orders m/m': '🏭 核心耐用品订单 月率',
        # 其他
        'Trade Balance': '📦 贸易差额',
        'Building Permits': '🏠 建筑许可',
        'Existing Home Sales': '🏠 成屋销售',
        'New Home Sales': '🏠 新屋销售',
    }
    
    # 影响等级映射
    impact_map = {
        'High': '高',
        'Medium': '中', 
        'Low': '低',
        'Holiday': '假日'
    }
    
    # 模块级缓存 (避免频繁请求导致429限流)
    global _macro_calendar_cache, _macro_calendar_cache_time
    
    now = datetime.now()
    if '_macro_calendar_cache' in dir(fetch_macro_calendar) and fetch_macro_calendar._cache_time:
        cache_age = (now - fetch_macro_calendar._cache_time).total_seconds()
        if cache_age < 1800 and fetch_macro_calendar._cache:  # 30分钟缓存
            return fetch_macro_calendar._cache
    
    try:
        # 获取本周和下周经济日历 (确保始终有upcoming事件)
        import time as _time
        calendar_urls = [
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
        ]
        all_events = []
        
        for url in calendar_urls:
            for attempt in range(2):
                try:
                    response = requests.get(
                        url,
                        timeout=15,
                        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
                    )
                    if response.status_code == 200:
                        all_events.extend(response.json())
                        break
                    elif response.status_code == 429:
                        _time.sleep(3 * (attempt + 1))
                    else:
                        print(f"⚠️ 经济日历 API 返回 {response.status_code} for {url}")
                        break
                except Exception as e:
                    print(f"⚠️ 经济日历请求失败: {e}")
                    break
            _time.sleep(0.5)
        
        events = all_events
        
        for event in events:
            country = event.get('country', '')
            title = event.get('title', '')
            impact = event.get('impact', '')
            date_str = event.get('date', '')
            actual = event.get('actual', '')
            forecast = event.get('forecast', '')
            previous = event.get('previous', '')
            
            # 只关注美元相关的高/中影响事件
            if country != 'USD':
                continue
            if impact not in ['High', 'Medium']:
                continue
            
            # 中文名称翻译
            chinese_name = name_translations.get(title, None)
            if chinese_name:
                display_name = chinese_name
            else:
                # 未翻译的事件添加默认图标
                if 'CPI' in title or 'Inflation' in title or 'PPI' in title or 'PCE' in title:
                    display_name = f'📊 {title}'
                elif 'Employ' in title or 'Unemployment' in title or 'Non-Farm' in title or 'NFP' in title:
                    display_name = f'👷 {title}'
                elif 'Fed' in title or 'FOMC' in title or 'Rate' in title or 'Powell' in title:
                    display_name = f'🏦 {title}'
                elif 'GDP' in title:
                    display_name = f'📈 {title}'
                elif 'Retail' in title or 'Consumer' in title:
                    display_name = f'🛒 {title}'
                elif 'ISM' in title or 'PMI' in title or 'Durable' in title:
                    display_name = f'🏭 {title}'
                else:
                    display_name = f'📅 {title}'
            
            # 解析时间 (转换为北京时间 UTC+8)
            try:
                event_time = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                beijing_tz = timezone(timedelta(hours=8))
                event_time_beijing = event_time.astimezone(beijing_tz)
                display_date = event_time_beijing.strftime("%m-%d %H:%M")
            except (ValueError, TypeError, AttributeError):
                display_date = date_str[:16] if len(date_str) > 16 else date_str
            
            # 判断事件是否已经过去（已公布）
            is_past = False
            try:
                event_dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                beijing_tz_check = timezone(timedelta(hours=8))
                now_beijing = datetime.now(beijing_tz_check)
                is_past = event_dt < now_beijing
            except (ValueError, TypeError):
                pass
            
            # 构建数据结果字符串
            data_result = ""
            if actual:
                data_result = f"公布: {actual}"
                if forecast:
                    data_result += f" · 预期: {forecast}"
                if previous:
                    data_result += f" · 前值: {previous}"
            elif is_past:
                parts = []
                if forecast:
                    parts.append(f"预期: {forecast}")
                if previous:
                    parts.append(f"前值: {previous}")
                data_result = " · ".join(parts) if parts else ""
            else:
                parts = []
                if forecast:
                    parts.append(f"预期: {forecast}")
                if previous:
                    parts.append(f"前值: {previous}")
                data_result = " · ".join(parts) if parts else ""
            
            # 事件状态
            if actual:
                event_status = "已公布"
            elif is_past:
                event_status = "已公布"
            else:
                event_status = "待公布"
            
            calendar.append({
                "event": display_name,
                "date": display_date,
                "data": data_result,
                "impact": impact_map.get(impact, ''),
                "type": "宏观经济",
                "has_actual": bool(actual),
                "is_past": is_past,
                "event_status": event_status,
                "forecast": forecast or "",
                "previous": previous or "",
                "actual": actual or ""
            })
        
        # 按时间排序
        calendar.sort(key=lambda x: x.get('date', ''))
        
        # 限制返回数量
        calendar = calendar[:15]
                    
    except Exception as e:
        print(f"⚠️ 经济日历 API 失败: {e}")
    
    # 如果没有获取到数据，返回备用信息
    if not calendar:
        calendar.append({
            "event": "📅 查看完整经济日历",
            "date": "",
            "data": "",
            "impact": "",
            "type": "链接",
            "url": "https://www.investing.com/economic-calendar/"
        })
    
    return calendar

