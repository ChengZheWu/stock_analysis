"""集中管理所有參數。策略參數採用文獻標準值，v1 不做最佳化。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
RAW = DATA / "raw"
CACHE = DATA / "cache"
RESULTS = DATA / "results"

for _d in (DATA, RAW, CACHE, RESULTS, RAW / "price", RAW / "dividend", RAW / "reduction"):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- 資料範圍
# 回測需要 10 年，指標暖身另外需要約 1 年（200 日均線、52 週高低點）
BACKFILL_START = "2014-09-01"
HISTORY_START = "2015-09-01"   # 指標暖身起點
BACKTEST_START = "2016-09-01"  # 10 年回測起點

TAIEX_ID = "TAIEX"

# ---------------------------------------------------------------- 股票池
UNIVERSE_TYPES = ("twse", "tpex")      # 只要上市、上櫃
EXCLUDE_INDUSTRIES = (
    "ETF", "ETN", "Index", "上櫃ETF", "上櫃指數股票型基金(ETF)",
    "指數投資證券(ETN)", "存託憑證", "所有證券", "大盤",
)

# ---------------------------------------------------------------- 資金與成本
INITIAL_CAPITAL = 500_000      # 回測用的假設本金
# 同時持股檔數上限（同一檔加碼不另佔名額）。
#
# 不設上限時實際會持有 16～28 檔，管理負擔偏高。設為 15 檔後實際平均 11.7 檔，
# 十年年化由 35.01% 降為 31.34%，但最大回撤由 -33.57% 改善為 -29.61%，
# 夏普值幾乎不變（1.44 → 1.42）——效率相當，只是規模較小。
# 不建議降到 10 檔：年化僅 19.32%，反而輸給 0050 的 23.86%，
# 動能策略需要夠多標的才能讓少數大贏家發揮作用。
MAX_POSITIONS = 15
POSITION_PCT = 1.0 / 15        # 每筆約占淨值的比例（回測用；實際買多少由使用者決定）
MAX_NEW_PER_DAY = 10           # 單日最多新進場筆數
MIN_POSITION_VALUE = 5_000     # 單筆最低進場金額，低於此不下單
# 單一檔最多買進幾次（加碼上限）。加碼後成本以加權平均計算，出場時全數賣出。
MAX_UNITS_PER_STOCK = 4
ALLOW_ODD_LOT = True           # 可買零股

FEE_RATE = 0.001425            # 券商手續費
FEE_DISCOUNT = 0.6             # 電子下單折扣，可調
FEE_MIN = 1                    # 零股最低手續費
TAX_RATE = 0.003               # 賣出證交稅
SLIPPAGE = 0.001               # 滑價假設

# ---------------------------------------------------------------- 策略參數
MKT_FILTER_MA = 200            # 大盤濾網：加權指數 200 日均線

TT_MA_SHORT, TT_MA_MID, TT_MA_LONG = 50, 150, 200   # 趨勢模板均線
TT_MA_LONG_RISING_DAYS = 20        # 200 日均線至少上升一個月
TT_MIN_ABOVE_52W_LOW = 0.30        # 距 52 週低點至少 +30%
TT_MAX_BELOW_52W_HIGH = 0.25       # 距 52 週高點不超過 -25%

MOM_LONG, MOM_MID, MOM_SHORT = 252, 126, 63   # 12 / 6 / 3 個月
MOM_SKIP = 21                                  # 排除最近 1 個月
MOM_WEIGHTS = (0.5, 0.3, 0.2)                  # 長中短權重

RANK_TOP_N = 10                # 每日最多推薦 10 支

BREAKOUT_LOOKBACK = 20         # 進場：突破 20 日新高
# 觸發價與現價的最大距離（以 ATR 計）。真正的突破型態應在高點附近盤整，
# 若股價已大幅回檔、離 20 日高點很遠，就不算突破待發，掛單也幾乎不會成交。
#
# 取值 2.0 的理由是可執行性與成交真實性，不是回測報酬：
# 0.5 過嚴，各窗口表現都最差；不設限則會掛出離現價 20% 以上的單子，
# 這種單子成交當天必為暴漲，現實中可能漲停鎖死根本買不到。
# 1.5～3.0 之間的報酬差異多為雜訊，不足以作為選值依據。
ENTRY_MAX_ATR_GAP = 2.0
ATR_PERIOD = 14
ATR_INIT_STOP = 2.5            # 初始停損 = 進場價 - 2.5 ATR
# 移動停損 = 持有期間最高「收盤」- 4.0 ATR。
#
# 經典的 Chandelier Exit 是「最高價 - 3 ATR」；本系統用最高收盤，
# 而收盤通常低於當日最高約 0.3～0.5 ATR，因此 3.0 會比經典版更緊。
# 4.0 才大致等同經典設定，這是選值的理由，不是回測報酬。
# 掃描結果也支持：3.0 在 10 年、5 年、1 年三個窗口都是最差的一組，
# 而 4.0 以上在三個窗口的報酬、夏普、回撤同時改善。
ATR_TRAIL_STOP = 4.0
EXIT_MA = 50                   # 收盤跌破 50 日均線出場

# ---------------------------------------------------------------- 回測窗口
BACKTEST_WINDOWS = {"10y": 10, "5y": 5, "1y": 1}
BENCHMARK = "0050"


def check_sizing(capital: float = INITIAL_CAPITAL) -> list[str]:
    """檢查資金與部位參數是否相容。

    小額本金容易踩到陷阱：若「每筆配置金額」低於最低進場金額，
    所有訊號都會被擋下，回測會安靜地產生零筆交易。
    """
    warnings = []
    per_trade = capital * POSITION_PCT
    if per_trade < MIN_POSITION_VALUE:
        need = MIN_POSITION_VALUE / capital
        warnings.append(
            f"每筆配置 {per_trade:,.0f} 元低於最低進場 {MIN_POSITION_VALUE:,} 元，"
            f"將無法成交任何一筆。請把 POSITION_PCT 提高到 {need:.0%} 以上"
            f"（同時持股數將降為約 {int(1/need)} 檔），或調降 MIN_POSITION_VALUE。"
        )
    if MAX_POSITIONS is not None and POSITION_PCT * MAX_POSITIONS > 1.0:
        warnings.append(
            f"POSITION_PCT × MAX_POSITIONS = {POSITION_PCT*MAX_POSITIONS:.0%} 超過 100%，"
            "實際持股數會受現金限制而達不到上限。"
        )
    if MAX_POSITIONS is None:
        warnings.append(
            f"未設持股上限，實際檔數由資金決定，約 {int(1/POSITION_PCT)} 檔"
            f"（每筆佔淨值 {POSITION_PCT:.1%}）。"
        )
    return warnings
