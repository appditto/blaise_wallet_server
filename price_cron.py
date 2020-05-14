# Intended to run as a standard cron job
# Will fetch prices and save to the local redis instance
import redis, json, time, sys, requests, os

rdata = redis.StrictRedis(host=os.getenv('REDIS_HOST', 'localhost'), port=6379, db=int(os.getenv('REDIS_DB', '2')))

currency_list = [ "ARS", "AUD", "BRL", "BTC", "CAD", "CHF", "CLP", "CNY", "CZK", "DKK", "EUR", "GBP", "HKD", "HUF", "IDR", "ILS", "INR", "JPY", "KRW", "MXN", "MYR", "NOK", "NZD", "PHP", "PKR", "PLN", "RUB", "SEK", "SGD", "THB", "TRY", "TWD", "USD", "ZAR", "SAR", "AED", "KWD" ]

coingecko_url='https://api.coingecko.com/api/v3/coins/pascalcoin?localization=false&tickers=false&market_data=true&community_data=false&developer_data=false&sparkline=false'

def coingecko():
	response = requests.get(url=coingecko_url).json()
	if 'market_data' not in response:
		return
	for currency in currency_list:
		try:
			data_name = currency.lower()
			price_currency = response['market_data']['current_price'][data_name]
			print(rdata.hset("prices", "coingecko:pasc-"+data_name, price_currency),"Coingecko PASC-"+currency, price_currency)
		except Exception:
			exc_type, exc_obj, exc_tb = sys.exc_info()
			print('exception',exc_type, exc_obj, exc_tb.tb_lineno)
			print("Failed to get price for PASC-"+currency.upper()+" Error")
	print(rdata.hset("prices", "coingecko:lastupdate",int(time.time())),int(time.time()))

coingecko()

print("Coingecko PASC-USD:", rdata.hget("prices", "coingecko:pasc-usd").decode('utf-8'))
print("Coingecko PASC-BTC:", rdata.hget("prices", "coingecko:pasc-btc").decode('utf-8'))
print("Last Update:          ", rdata.hget("prices", "coingecko:lastupdate").decode('utf-8'))
