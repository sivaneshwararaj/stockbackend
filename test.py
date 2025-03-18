from tiingo import TiingoClient
# Set TIINGO_API_KEY in your environment variables in your .bash_profile, OR
# pass a dictionary with 'api_key' as a key into the TiingoClient.


config = {}

# To reuse the same HTTP Session across API calls (and have better performance), include a session key.
config['session'] = True

# If you don't have your API key as an environment variable,
# pass it in via a configuration dictionary.
config['api_key'] = "d1fbec3a53dc45759eabd6923f490cf921708e5a"

# Initialize
client = TiingoClient(config)

fundamentals_daily = client.get_fundamentals_daily('NVDA',
                                        startDate='2025-03-01',
                                        endDate='2025-03-02')
print(fundamentals_daily)
