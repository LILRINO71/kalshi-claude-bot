"""The stocks the pre-market scanner looks at: liquid US names that day traders actually trade.

Each entry is ticker -> (company name used to match news headlines, sector). Edit freely, or set
`universe` in data/daytrader/settings.json to a list of tickers to scan only those.
"""
STOCKS = {
    # Mega-cap tech and AI
    "AAPL": ("Apple", "Technology"), "MSFT": ("Microsoft", "Technology"), "NVDA": ("Nvidia", "Semiconductors"),
    "AMZN": ("Amazon", "Consumer"), "GOOGL": ("Alphabet|Google", "Communication"), "META": ("Meta Platforms|Facebook", "Communication"),
    "TSLA": ("Tesla", "Autos"), "AVGO": ("Broadcom", "Semiconductors"), "ORCL": ("Oracle", "Technology"),
    "NFLX": ("Netflix", "Communication"), "ADBE": ("Adobe", "Technology"), "CRM": ("Salesforce", "Technology"),
    "AMD": ("AMD|Advanced Micro Devices", "Semiconductors"), "INTC": ("Intel", "Semiconductors"),
    "QCOM": ("Qualcomm", "Semiconductors"), "MU": ("Micron", "Semiconductors"), "ARM": ("Arm Holdings", "Semiconductors"),
    "TSM": ("TSMC|Taiwan Semiconductor", "Semiconductors"), "ASML": ("ASML", "Semiconductors"),
    "SMCI": ("Super Micro|Supermicro", "Technology"), "DELL": ("Dell", "Technology"), "IBM": ("IBM", "Technology"),
    "NOW": ("ServiceNow", "Technology"), "SNOW": ("Snowflake", "Technology"), "PLTR": ("Palantir", "Technology"),
    "CRWD": ("CrowdStrike", "Technology"), "PANW": ("Palo Alto Networks", "Technology"), "NET": ("Cloudflare", "Technology"),
    "DDOG": ("Datadog", "Technology"), "SHOP": ("Shopify", "Technology"), "UBER": ("Uber", "Technology"),
    "ABNB": ("Airbnb", "Consumer"), "COIN": ("Coinbase", "Financials"), "HOOD": ("Robinhood", "Financials"),
    "MSTR": ("MicroStrategy|Strategy Inc", "Technology"), "MARA": ("Marathon Digital|MARA Holdings", "Technology"),
    "RIOT": ("Riot Platforms", "Technology"), "SOFI": ("SoFi", "Financials"), "PYPL": ("PayPal", "Financials"),
    "XYZ": ("Block Inc|Square|Cash App", "Financials"), "RBLX": ("Roblox", "Communication"), "SNAP": ("Snap Inc|Snapchat", "Communication"),
    "PINS": ("Pinterest", "Communication"), "SPOT": ("Spotify", "Communication"), "DIS": ("Disney", "Communication"),
    "RDDT": ("Reddit", "Communication"), "APP": ("AppLovin", "Technology"), "ANET": ("Arista", "Technology"),
    "MRVL": ("Marvell", "Semiconductors"), "ON": ("ON Semiconductor|onsemi", "Semiconductors"),
    "IONQ": ("IonQ", "Technology"), "RGTI": ("Rigetti", "Technology"), "SOUN": ("SoundHound", "Technology"),
    "AI": ("C3.ai", "Technology"), "U": ("Unity Software", "Technology"), "AFRM": ("Affirm", "Financials"),
    "UPST": ("Upstart", "Financials"), "CVNA": ("Carvana", "Consumer"), "RIVN": ("Rivian", "Autos"),
    "LCID": ("Lucid", "Autos"), "NIO": ("NIO", "Autos"), "F": ("Ford", "Autos"), "GM": ("General Motors", "Autos"),
    # Financials
    "JPM": ("JPMorgan", "Financials"), "BAC": ("Bank of America", "Financials"), "GS": ("Goldman Sachs", "Financials"),
    "MS": ("Morgan Stanley", "Financials"), "WFC": ("Wells Fargo", "Financials"), "C": ("Citigroup", "Financials"),
    "V": ("Visa", "Financials"), "MA": ("Mastercard", "Financials"), "BRK-B": ("Berkshire Hathaway", "Financials"),
    # Health care
    "LLY": ("Eli Lilly", "Health Care"), "UNH": ("UnitedHealth", "Health Care"), "JNJ": ("Johnson & Johnson", "Health Care"),
    "PFE": ("Pfizer", "Health Care"), "MRK": ("Merck", "Health Care"), "ABBV": ("AbbVie", "Health Care"),
    "NVO": ("Novo Nordisk", "Health Care"), "MRNA": ("Moderna", "Health Care"), "HIMS": ("Hims & Hers", "Health Care"),
    # Consumer and industrials
    "WMT": ("Walmart", "Consumer"), "COST": ("Costco", "Consumer"), "TGT": ("Target Corp", "Consumer"),
    "HD": ("Home Depot", "Consumer"), "NKE": ("Nike", "Consumer"), "SBUX": ("Starbucks", "Consumer"),
    "MCD": ("McDonald's", "Consumer"), "KO": ("Coca-Cola", "Consumer"), "PEP": ("PepsiCo", "Consumer"),
    "LULU": ("Lululemon", "Consumer"), "CMG": ("Chipotle", "Consumer"), "BA": ("Boeing", "Industrials"),
    "CAT": ("Caterpillar", "Industrials"), "GE": ("GE Aerospace|General Electric", "Industrials"),
    "LMT": ("Lockheed Martin", "Industrials"), "RTX": ("RTX Corp|Raytheon", "Industrials"),
    "UPS": ("UPS", "Industrials"), "FDX": ("FedEx", "Industrials"), "DAL": ("Delta Air Lines", "Industrials"),
    "UAL": ("United Airlines", "Industrials"), "CCL": ("Carnival", "Consumer"),
    # Energy and materials
    "XOM": ("Exxon", "Energy"), "CVX": ("Chevron", "Energy"), "OXY": ("Occidental", "Energy"),
    "FCX": ("Freeport", "Materials"), "NEM": ("Newmont", "Materials"), "VST": ("Vistra", "Utilities"),
    "CEG": ("Constellation Energy", "Utilities"), "OKLO": ("Oklo", "Utilities"), "SMR": ("NuScale", "Utilities"),
}

# Market context, not traded by default.
INDEX_ETFS = {"SPY": "S&P 500 ETF", "QQQ": "Nasdaq-100 ETF", "IWM": "Russell 2000 ETF"}


def name_of(ticker):
    return STOCKS.get(ticker, (ticker, ""))[0].split("|")[0]


def aliases(ticker):
    return STOCKS.get(ticker, (ticker, ""))[0].split("|")


def sector_of(ticker):
    return STOCKS.get(ticker, ("", "Unknown"))[1]
