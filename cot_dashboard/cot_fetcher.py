import requests
import json
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import sys
import io
import pandas as pd

# Устанавливаем UTF-8 для вывода в консоль
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

class COTDataFetcher:
    """Класс для получения данных COT из API CFTC"""

    def __init__(self):
        self.base_url = "https://publicreporting.cftc.gov/resource/"
        self.instruments = {
            'XAU (Золото)': {
                'dataset': '6dca-aqww.json',
                'code': '088691',
                'type': 'legacy'
            },
            'XAG (Серебро)': {
                'dataset': '6dca-aqww.json',
                'code': '084691',
                'type': 'legacy'
            },
            'EUR/USD': {
                'dataset': 'yw9f-hn96.json',
                'code': '099741',
                'type': 'tff'
            },
            'GBP/USD': {
                'dataset': 'yw9f-hn96.json',
                'code': '096742',
                'type': 'tff'
            },
            'USD/JPY': {
                'dataset': 'yw9f-hn96.json',
                'code': '097741',
                'type': 'tff'
            },
            'AUD/USD': {
                'dataset': 'yw9f-hn96.json',
                'code': '232741',
                'type': 'tff'
            }
        }
        self.price_tickers = {
            'XAU (Золото)': 'GC=F',
            'XAG (Серебро)': 'SI=F',
            'EUR/USD': 'EURUSD=X',
            'GBP/USD': 'GBPUSD=X',
            'USD/JPY': 'USDJPY=X',
            'AUD/USD': 'AUDUSD=X'
        }

    def fetch_latest_data(self, instrument_name, limit=12):
        """Получает последние данные для инструмента (по умолчанию 12 недель)"""
        config = self.instruments[instrument_name]
        url = f"{self.base_url}{config['dataset']}"
        params = {
            '$where': f"cftc_contract_market_code='{config['code']}'",
            '$order': 'report_date_as_yyyy_mm_dd DESC',
            '$limit': limit
        }

        try:
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            if not data:
                return None

            results = []
            for record in data:
                parsed = self._parse_record(record, config['type'], instrument_name)
                if parsed:
                    results.append(parsed)

            return results

        except Exception as e:
            print(f"Ошибка при получении данных для {instrument_name}: {e}")
            return None

    def fetch_historical_data(self, instrument_name, start_date, end_date, limit=500):
        """Получает исторические COT-данные за диапазон дат.

        start_date / end_date: строки 'YYYY-MM-DD'
        limit: макс. число записей (default 500 = ~10 лет)
        """
        config = self.instruments[instrument_name]
        url = f"{self.base_url}{config['dataset']}"
        where = (
            f"cftc_contract_market_code='{config['code']}'"
            f" AND report_date_as_yyyy_mm_dd >= '{start_date}'"
            f" AND report_date_as_yyyy_mm_dd <= '{end_date}'"
        )
        params = {
            '$where': where,
            '$order': 'report_date_as_yyyy_mm_dd ASC',
            '$limit': limit,
        }
        try:
            resp = requests.get(url, params=params, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                print(f"  [COT] No historical data for {instrument_name}")
                return []
            results = []
            for record in data:
                parsed = self._parse_record(record, config['type'], instrument_name)
                if parsed:
                    results.append(parsed)
            print(f"  [COT] {instrument_name}: {len(results)} records "
                  f"({results[0]['date']} -> {results[-1]['date']})")
            return results
        except Exception as e:
            print(f"  [COT] Error fetching history for {instrument_name}: {e}")
            return []

    def _parse_record(self, record, data_type, instrument_name):
        """Парсит запись в зависимости от типа данных"""
        result = {
            'instrument': instrument_name,
            'date': record.get('report_date_as_yyyy_mm_dd', '')[:10],
            'open_interest': int(record.get('open_interest_all', 0))
        }

        if data_type == 'legacy':
            # Для золота (Legacy format)
            comm_long = int(record.get('comm_positions_long_all', 0))
            comm_short = int(record.get('comm_positions_short_all', 0))
            noncomm_long = int(record.get('noncomm_positions_long_all', 0))
            noncomm_short = int(record.get('noncomm_positions_short_all', 0))

            result.update({
                'commercial_long': comm_long,
                'commercial_short': comm_short,
                'commercial_net': comm_long - comm_short,
                'speculative_long': noncomm_long,
                'speculative_short': noncomm_short,
                'speculative_net': noncomm_long - noncomm_short,
                'spec_long_pct': float(record.get('pct_of_oi_noncomm_long_all', 0)),
                'spec_short_pct': float(record.get('pct_of_oi_noncomm_short_all', 0))
            })

        elif data_type == 'tff':
            # Для валют (TFF format)
            lev_long = int(record.get('lev_money_positions_long', 0))
            lev_short = int(record.get('lev_money_positions_short', 0))
            asset_long = int(record.get('asset_mgr_positions_long', 0))
            asset_short = int(record.get('asset_mgr_positions_short', 0))

            result.update({
                'leveraged_long': lev_long,
                'leveraged_short': lev_short,
                'leveraged_net': lev_long - lev_short,
                'asset_mgr_long': asset_long,
                'asset_mgr_short': asset_short,
                'asset_mgr_net': asset_long - asset_short,
                'lev_long_pct': float(record.get('pct_of_oi_lev_money_long', 0)),
                'lev_short_pct': float(record.get('pct_of_oi_lev_money_short', 0))
            })

        return result

    def fetch_all_instruments(self):
        """Получает данные для всех инструментов параллельно"""
        all_data = {}

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(self.fetch_latest_data, instrument, 12): instrument
                for instrument in self.instruments.keys()
            }
            for future in as_completed(futures):
                instrument = futures[future]
                try:
                    data = future.result()
                    if data:
                        all_data[instrument] = data
                except Exception as e:
                    print(f"Ошибка при получении {instrument}: {e}")

        return all_data

    def calculate_changes(self, current, previous):
        """Вычисляет изменения между текущими и предыдущими данными"""
        if not current or not previous:
            return None

        changes = {
            'date_current': current['date'],
            'date_previous': previous['date']
        }

        # Вычисляем изменения для всех числовых полей
        for key in current.keys():
            if key not in ['instrument', 'date'] and isinstance(current[key], (int, float)):
                prev_val = previous.get(key, 0)
                curr_val = current[key]
                change = curr_val - prev_val
                changes[f'{key}_change'] = change

                # Процентное изменение
                if prev_val != 0:
                    pct_change = (change / abs(prev_val)) * 100
                    changes[f'{key}_pct_change'] = round(pct_change, 2)

        return changes

    def save_data(self, filename='cot_data.json'):
        """Сохраняет данные в JSON файл"""
        raw_data = self.fetch_all_instruments()

        result = {}

        for instrument, records in raw_data.items():
            entry = {'records': records}

            if len(records) >= 2:
                entry['changes'] = self.calculate_changes(records[0], records[1])
                entry['analysis'] = self.advanced_analysis(instrument, records)
            else:
                entry['changes'] = None
                entry['analysis'] = None

            result[instrument] = entry

        result['metadata'] = {
            'last_updated': datetime.now().isoformat(),
            'next_update': self._get_next_update_date().isoformat()
        }

        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        return result

    def advanced_analysis(self, instrument_name, records):
        """Расширенный анализ: дивергенции, анализ OI, сентимент"""
        if len(records) < 2:
            return None

        analysis = {}
        current = records[0]

        # 0. Общий сентимент (чтобы фронтенд не вычислял)
        if 'speculative_net' in current:
            net_pos = current['speculative_net']
        elif 'leveraged_net' in current:
            net_pos = current['leveraged_net']
        else:
            net_pos = 0

        analysis['sentiment'] = {
            'net_position': net_pos,
            'direction': 'bullish' if net_pos > 0 else 'bearish',
            'text': f"{'Бычий' if net_pos > 0 else 'Медвежий'} {'🟢' if net_pos > 0 else '🔴'}"
        }

        # 1. Анализ открытого интереса (Open Interest)
        oi_analysis = self.analyze_open_interest(records)
        analysis['open_interest_analysis'] = oi_analysis

        # 2. Определение тренда позиций
        trend = self.determine_trend(records)
        analysis['trend'] = trend

        # 3. Дивергенция трейдеров (Smart Money vs Crowd)
        analysis['trader_divergence'] = self.analyze_trader_divergence(records)

        # 4. Дивергенция цены и позиций
        dates = []
        for r in records[:2]:
            try:
                dates.append(datetime.strptime(r['date'], '%Y-%m-%d').date())
            except (ValueError, KeyError):
                pass

        if len(dates) == 2:
            price_data = self.fetch_price_data(instrument_name, dates)
            analysis['price_divergence'] = self.analyze_price_divergence(records, price_data)
        else:
            analysis['price_divergence'] = {
                'signal': 'neutral',
                'interpretation': 'Недостаточно данных о датах',
                'divergence_type': 'none'
            }

        # 5. Итоговый вердикт
        analysis['verdict'] = self._compute_verdict(analysis)

        # 6. JPY inversion: COT tracks JPY futures → flip for USD/JPY trading
        if 'JPY' in instrument_name:
            inv = {'bullish': 'bearish', 'bearish': 'bullish',
                   'strong_bullish': 'strong_bearish', 'strong_bearish': 'strong_bullish'}
            inv_text = {'Бычий 🟢': 'Медвежий 🔴', 'Медвежий 🔴': 'Бычий 🟢',
                        'Бычий': 'Медвежий', 'Медвежий': 'Бычий'}
            # Sentiment
            s = analysis.get('sentiment', {})
            if s.get('direction'):
                s['direction'] = inv.get(s['direction'], s['direction'])
                s['text'] = inv_text.get(s['text'], s['text'])
            # Verdict
            v = analysis.get('verdict', {})
            if v.get('signal'):
                v['signal'] = inv.get(v['signal'], v['signal'])
                v['score'] = -v.get('score', 0)
                # Fix verdict text
                txt = v.get('text', '')
                txt = txt.replace('бычий', '%%TEMP%%').replace('медвежий', 'бычий').replace('%%TEMP%%', 'медвежий')
                txt = txt.replace('Бычий', '%%TEMP%%').replace('Медвежий', 'Бычий').replace('%%TEMP%%', 'Медвежий')
                v['text'] = txt
            # Trend direction
            trend = analysis.get('trend') or {}
            if trend.get('direction') in ('up', 'down'):
                trend['direction'] = 'down' if trend['direction'] == 'up' else 'up'
            # Trader divergence
            trader = analysis.get('trader_divergence') or {}
            if trader.get('signal') == 'bullish_divergence':
                trader['signal'] = 'bearish_divergence'
            elif trader.get('signal') == 'bearish_divergence':
                trader['signal'] = 'bullish_divergence'
            # Price divergence
            price_div = analysis.get('price_divergence') or {}
            if price_div.get('signal') == 'bullish_divergence':
                price_div['signal'] = 'bearish_divergence'
            elif price_div.get('signal') == 'bearish_divergence':
                price_div['signal'] = 'bullish_divergence'

        return analysis

    def _compute_verdict(self, analysis):
        """Агрегирует все метрики в единый торговый вердикт"""
        score = 0
        reasons = []

        # OI Analysis
        oi = analysis.get('open_interest_analysis', {})
        oi_signal = oi.get('signal', 'neutral')
        if oi_signal == 'strong_bullish':
            score += 2; reasons.append('приток денег в лонги')
        elif oi_signal == 'weak_bullish':
            score += 1; reasons.append('слабый приток в лонги')
        elif oi_signal == 'strong_bearish':
            score -= 2; reasons.append('приток денег в шорты')
        elif oi_signal == 'weak_bearish':
            score -= 1; reasons.append('слабый приток в шорты')

        # Trader Divergence
        trader = analysis.get('trader_divergence', {})
        if trader and trader.get('signal') == 'bullish_divergence':
            score += 2; reasons.append('умные деньги покупают')
        elif trader and trader.get('signal') == 'bearish_divergence':
            score -= 2; reasons.append('умные деньги продают')

        # Price Divergence
        price = analysis.get('price_divergence', {})
        if price and price.get('signal') == 'bullish_divergence':
            score += 2; reasons.append('цена падает, позиции растут')
        elif price and price.get('signal') == 'bearish_divergence':
            score -= 2; reasons.append('цена растёт, позиции падают')

        # Trend
        trend = analysis.get('trend', {})
        if trend:
            if trend.get('direction') == 'up':
                score += 1; reasons.append('тренд позиций вверх')
            elif trend.get('direction') == 'down':
                score -= 1; reasons.append('тренд позиций вниз')

        # Sentiment baseline
        sent = analysis.get('sentiment', {})
        if sent.get('direction') == 'bullish':
            score += 1
        else:
            score -= 1

        if score >= 5:
            text = "Сильный бычий консенсус"
            signal = 'strong_bullish'
        elif score >= 2:
            text = "Умеренно бычий"
            signal = 'bullish'
        elif score >= -1:
            text = "Нейтральный / разнонаправленный"
            signal = 'neutral'
        elif score >= -4:
            text = "Умеренно медвежий"
            signal = 'bearish'
        else:
            text = "Сильный медвежий консенсус"
            signal = 'strong_bearish'

        return {
            'score': score,
            'text': text,
            'signal': signal,
            'reasons': reasons
        }

    def analyze_open_interest(self, records):
        """Анализирует открытый интерес"""
        if len(records) < 2:
            return None

        current = records[0]
        previous = records[1]

        oi_current = current.get('open_interest', 0)
        oi_previous = previous.get('open_interest', 0)
        oi_change = oi_current - oi_previous
        oi_change_pct = (oi_change / oi_previous * 100) if oi_previous != 0 else 0

        # Определяем тренд нетто-позиций
        if 'speculative_net' in current:
            net_current = current['speculative_net']
            net_previous = previous['speculative_net']
        elif 'leveraged_net' in current:
            net_current = current['leveraged_net']
            net_previous = previous['leveraged_net']
        else:
            return None

        net_change = net_current - net_previous

        net_is_long = net_current > 0
        pos_label = 'нетто-лонг' if net_is_long else 'нетто-шорт'

        if oi_change > 0 and net_change > 0:
            interpretation = (f"Приток новых денег в лонги: OI вырос на +{oi_change:,}, "
                              f"нетто-позиция увеличилась на +{net_change:,}. "
                              f"Новые контракты открываются преимущественно в покупку — бычий недельный сдвиг.")
            signal = "strong_bullish"
        elif oi_change > 0 and net_change < 0:
            interpretation = (f"Приток новых денег в шорты: OI вырос на +{oi_change:,}, "
                              f"но нетто-позиция снизилась на {net_change:,}. "
                              f"Новые контракты открываются преимущественно в продажу — медвежий недельный сдвиг. "
                              f"При этом общая позиция остаётся {pos_label} ({net_current:,}).")
            signal = "strong_bearish"
        elif oi_change < 0 and net_change > 0:
            interpretation = (f"Закрытие шортов или фиксация прибыли: OI снизился на {oi_change:,}, "
                              f"но нетто выросла на +{net_change:,}. "
                              f"Рынок сжимается, но позиции смещаются в лонги — слабый бычий сигнал.")
            signal = "weak_bullish"
        elif oi_change < 0 and net_change < 0:
            interpretation = (f"Закрытие лонгов или выход из позиций: OI снизился на {oi_change:,}, "
                              f"нетто упала на {net_change:,}. "
                              f"Рынок сжимается, позиции смещаются в шорты — слабый медвежий сигнал.")
            signal = "weak_bearish"
        else:
            interpretation = "Без существенных изменений за неделю — нейтральный сигнал."
            signal = "neutral"

        return {
            'oi_current': oi_current,
            'oi_change': oi_change,
            'oi_change_pct': round(oi_change_pct, 2),
            'net_change': net_change,
            'net_current': net_current,
            'interpretation': interpretation,
            'signal': signal
        }

    def determine_trend(self, records):
        """Определяет тренд позиций за последние недели"""
        if len(records) < 4:
            return None

        # Берем последние 4 недели
        recent = records[:4]

        if 'speculative_net' in recent[0]:
            net_key = 'speculative_net'
        elif 'leveraged_net' in recent[0]:
            net_key = 'leveraged_net'
        else:
            return None

        positions = [r[net_key] for r in recent]

        # Простой анализ тренда
        increases = sum(1 for i in range(len(positions)-1) if positions[i] > positions[i+1])

        if increases >= 3:
            trend = f"Устойчивый рост позиций ({increases}/4 недель вверх)"
            direction = "up"
        elif increases == 0:
            trend = f"Устойчивое снижение позиций (4/4 недель вниз)"
            direction = "down"
        else:
            trend = f"Разнонаправленное движение ({increases}/4 недель вверх, без явного тренда)"
            direction = "sideways"

        return {
            'description': trend,
            'direction': direction,
            'weeks_analyzed': len(positions)
        }


    def fetch_price_data(self, instrument_name, dates):
        """Получает цены закрытия для двух дат через Yahoo Finance"""
        if not YFINANCE_AVAILABLE:
            return None

        ticker = self.price_tickers.get(instrument_name)
        if not ticker:
            return None

        try:
            start = dates[1] - timedelta(days=5)
            end = dates[0] + timedelta(days=5)

            df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)

            if df.empty:
                return None

            close_series = df['Close'].squeeze()

            closes = {}
            for d in dates:
                target = pd.Timestamp(d)
                if target in close_series.index:
                    closes[d.isoformat()] = float(close_series.loc[target].item() if hasattr(close_series.loc[target], 'item') else close_series.loc[target])
                else:
                    diffs = abs(close_series.index - target)
                    nearest_idx = diffs.argmin()
                    diff_days = abs((close_series.index[nearest_idx] - target).days)
                    if diff_days <= 3:
                        val = close_series.iloc[nearest_idx]
                        closes[d.isoformat()] = float(val.item() if hasattr(val, 'item') else val)

            return closes if len(closes) == 2 else None

        except Exception as e:
            print(f"Ошибка получения цен для {instrument_name}: {e}")
            return None

    def analyze_trader_divergence(self, records):
        """Анализ дивергенции между 'умными деньгами' и спекулянтами"""
        current = records[0]
        previous = records[1]

        if 'commercial_net' in current:
            smart_key = 'commercial_net'
            crowd_key = 'speculative_net'
            smart_label = 'Commercials (хеджеры)'
            crowd_label = 'Speculators (спекулянты)'
        elif 'asset_mgr_net' in current:
            smart_key = 'asset_mgr_net'
            crowd_key = 'leveraged_net'
            smart_label = 'Asset Managers (институционалы)'
            crowd_label = 'Leveraged Funds (фонды)'
        else:
            return None

        smart_change = current[smart_key] - previous[smart_key]
        crowd_change = current[crowd_key] - previous[crowd_key]

        if smart_change > 0 and crowd_change < 0:
            divergence_type = 'bullish'
            signal = 'bullish_divergence'
            interpretation = (
                f"{smart_label} наращивают позиции (+{smart_change:,}), "
                f"а {crowd_label} сокращают ({crowd_change:,}) — "
                f"бычья дивергенция: умные деньги покупают, пока толпа продаёт. "
                f"Исторически такой расклад часто предшествует развороту вверх."
            )
        elif smart_change < 0 and crowd_change > 0:
            divergence_type = 'bearish'
            signal = 'bearish_divergence'
            interpretation = (
                f"{smart_label} сокращают позиции ({smart_change:,}), "
                f"а {crowd_label} наращивают (+{crowd_change:,}) — "
                f"медвежья дивергенция: умные деньги продают, пока толпа покупает. "
                f"Исторически такой расклад часто предшествует развороту вниз."
            )
        elif smart_change >= 0 and crowd_change >= 0:
            divergence_type = 'none'
            signal = 'no_divergence'
            interpretation = (
                f"Обе группы наращивают позиции: {smart_label} +{smart_change:,}, "
                f"{crowd_label} +{crowd_change:,}. "
                f"Консенсус в покупку — тренд подтверждается, но нет контрарианского сигнала."
            )
        else:
            divergence_type = 'none'
            signal = 'no_divergence'
            interpretation = (
                f"Обе группы сокращают позиции: {smart_label} {smart_change:,}, "
                f"{crowd_label} {crowd_change:,}. "
                f"Общий выход из позиций — консенсус в продажу, но нет контрарианского сигнала."
            )

        return {
            'signal': signal,
            'interpretation': interpretation,
            'smart_money_change': smart_change,
            'crowd_change': crowd_change,
            'divergence_type': divergence_type,
            'smart_label': smart_label,
            'crowd_label': crowd_label
        }

    def analyze_price_divergence(self, records, price_data):
        """Анализ дивергенции между ценой и позициями"""
        if price_data is None:
            return {
                'signal': 'neutral',
                'interpretation': 'Данные о цене недоступны',
                'divergence_type': 'none'
            }

        current = records[0]
        previous = records[1]

        date_curr = current['date']
        date_prev = previous['date']

        if date_curr not in price_data or date_prev not in price_data:
            return {
                'signal': 'neutral',
                'interpretation': 'Нет данных о цене за нужные даты',
                'divergence_type': 'none'
            }

        close_curr = price_data[date_curr]
        close_prev = price_data[date_prev]
        price_change = close_curr - close_prev
        price_change_pct = (price_change / close_prev) * 100

        if 'speculative_net' in current:
            pos_key = 'speculative_net'
        elif 'leveraged_net' in current:
            pos_key = 'leveraged_net'
        else:
            return None

        pos_change = current[pos_key] - previous[pos_key]

        if price_change > 0 and pos_change < 0:
            divergence_type = 'bearish'
            signal = 'bearish_divergence'
            interpretation = (
                f"Цена выросла на +{price_change_pct:.1f}%, "
                f"но нетто-позиции сократились на {pos_change:,} — "
                f"медвежья дивергенция: рост цены не поддержан притоком позиций. "
                f"Покупателей становится меньше на фоне роста — риск разворота вниз."
            )
        elif price_change < 0 and pos_change > 0:
            divergence_type = 'bullish'
            signal = 'bullish_divergence'
            interpretation = (
                f"Цена снизилась на {price_change_pct:.1f}%, "
                f"но нетто-позиции выросли на +{pos_change:,} — "
                f"бычья дивергенция: падение цены не сопровождается бегством из позиций. "
                f"Крупные игроки накапливают на просадке — возможен разворот вверх."
            )
        elif price_change > 0 and pos_change > 0:
            divergence_type = 'none'
            signal = 'confirming'
            interpretation = (
                f"Цена (+{price_change_pct:.1f}%) и позиции (+{pos_change:,}) растут синхронно — "
                f"бычий тренд подтверждается: приток позиций поддерживает рост цены."
            )
        elif price_change < 0 and pos_change < 0:
            divergence_type = 'none'
            signal = 'confirming'
            interpretation = (
                f"Цена ({price_change_pct:.1f}%) и позиции ({pos_change:,}) падают синхронно — "
                f"медвежий тренд подтверждается: сокращение позиций усиливает снижение цены."
            )
        else:
            divergence_type = 'none'
            signal = 'confirming'
            interpretation = "Цена и позиции без существенных изменений — нейтральный сигнал."

        return {
            'signal': signal,
            'interpretation': interpretation,
            'price_change': round(price_change, 2),
            'price_change_pct': round(price_change_pct, 2),
            'position_change': pos_change,
            'divergence_type': divergence_type
        }

    def _get_next_update_date(self):
        """Вычисляет дату следующего обновления (следующее воскресенье)"""
        today = datetime.now()
        days_until_sunday = (6 - today.weekday()) % 7
        if days_until_sunday == 0:
            days_until_sunday = 7
        next_sunday = today + timedelta(days=days_until_sunday)
        return next_sunday.replace(hour=10, minute=0, second=0, microsecond=0)

if __name__ == "__main__":
    fetcher = COTDataFetcher()
    print("Получение данных COT...")
    data = fetcher.save_data()
    print(f"✓ Данные сохранены в cot_data.json")
    print(f"✓ Следующее обновление: {data['metadata']['next_update']}")
