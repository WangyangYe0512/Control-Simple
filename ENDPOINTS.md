Available endpoints¶
If you wish to call the REST API manually via another route, e.g. directly via curl, the table below shows the relevant URL endpoints and parameters. All endpoints in the below table need to be prefixed with the base URL of the API, e.g. http://127.0.0.1:8080/api/v1/ - so the command becomes http://127.0.0.1:8080/api/v1/<command>.

Endpoint	Method	Description / Parameters
/ping	GET	Simple command testing the API Readiness - requires no authentication.
/start	POST	Starts the trader.
/pause	POST	Pause the trader. Gracefully handle open trades according to their rules. Do not enter new positions.
/stop	POST	Stops the trader.
/stopbuy	POST	Stops the trader from opening new trades. Gracefully closes open trades according to their rules.
/reload_config	POST	Reloads the configuration file.
/trades	GET	List last trades. Limited to 500 trades per call.
/trade/<tradeid>	GET	Get specific trade.
Params:
- tradeid (int)
/trades/<tradeid>	DELETE	Remove trade from the database. Tries to close open orders. Requires manual handling of this trade on the exchange.
Params:
- tradeid (int)
/trades/<tradeid>/open-order	DELETE	Cancel open order for this trade.
Params:
- tradeid (int)
/trades/<tradeid>/reload	POST	Reload a trade from the Exchange. Only works in live, and can potentially help recover a trade that was manually sold on the exchange.
Params:
- tradeid (int)
/show_config	GET	Shows part of the current configuration with relevant settings to operation.
/logs	GET	Shows last log messages.
/status	GET	Lists all open trades.
/count	GET	Displays number of trades used and available.
/entries	GET	Shows profit statistics for each enter tags for given pair (or all pairs if pair isn't given). Pair is optional.
Params:
- pair (str)
/exits	GET	Shows profit statistics for each exit reasons for given pair (or all pairs if pair isn't given). Pair is optional.
Params:
- pair (str)
/mix_tags	GET	Shows profit statistics for each combinations of enter tag + exit reasons for given pair (or all pairs if pair isn't given). Pair is optional.
Params:
- pair (str)
/locks	GET	Displays currently locked pairs.
/locks	POST	Locks a pair until "until". (Until will be rounded up to the nearest timeframe). Side is optional and is either long or short (default is long). Reason is optional.
Params:
- <pair> (str)
- <until> (datetime)
- [side] (str)
- [reason] (str)
/locks/<lockid>	DELETE	Deletes (disables) the lock by id.
Params:
- lockid (int)
/profit	GET	Display a summary of your profit/loss from close trades and some stats about your performance.
/forceexit	POST	Instantly exits the given trade (ignoring minimum_roi), using the given order type ("market" or "limit", uses your config setting if not specified), and the chosen amount (full sell if not specified). If all is supplied as the tradeid, then all currently open trades will be forced to exit.
Params:
- <tradeid> (int or str)
- <ordertype> (str)
- [amount] (float)
/forceenter	POST	Instantly enters the given pair. Side is optional and is either long or short (default is long). Rate is optional. (force_entry_enable must be set to True)
Params:
- <pair> (str)
- <side> (str)
- [rate] (float)
/performance	GET	Show performance of each finished trade grouped by pair.
/balance	GET	Show account balance per currency.
/daily	GET	Shows profit or loss per day, over the last n days (n defaults to 7).
Params:
- <n> (int)
/weekly	GET	Shows profit or loss per week, over the last n days (n defaults to 4).
Params:
- <n> (int)
/monthly	GET	Shows profit or loss per month, over the last n days (n defaults to 3).
Params:
- <n> (int)
/stats	GET	Display a summary of profit / loss reasons as well as average holding times.
/whitelist	GET	Show the current whitelist.
/blacklist	GET	Show the current blacklist.
/blacklist	POST	Adds the specified pair to the blacklist.
Params:
- pair (str)
/blacklist	DELETE	Deletes the specified list of pairs from the blacklist.
Params:
- [pair,pair] (list[str])
/pair_candles	GET	Returns dataframe for a pair / timeframe combination while the bot is running. Alpha
/pair_candles	POST	Returns dataframe for a pair / timeframe combination while the bot is running, filtered by a provided list of columns to return. Alpha
Params:
- <column_list> (list[str])
/pair_history	GET	Returns an analyzed dataframe for a given timerange, analyzed by a given strategy. Alpha
/pair_history	POST	Returns an analyzed dataframe for a given timerange, analyzed by a given strategy, filtered by a provided list of columns to return. Alpha
Params:
- <column_list> (list[str])
/plot_config	GET	Get plot config from the strategy (or nothing if not configured). Alpha
/strategies	GET	List strategies in strategy directory. Alpha
/strategy/<strategy>	GET	Get specific Strategy content by strategy class name. Alpha
Params:
- <strategy> (str)
/available_pairs	GET	List available backtest data. Alpha
/version	GET	Show version.
/sysinfo	GET	Show information about the system load.
/health	GET	Show bot health (last bot loop).
