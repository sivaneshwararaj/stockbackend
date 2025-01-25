from __future__ import print_function
import asyncio
import time
import intrinio_sdk as intrinio
from intrinio_sdk.rest import ApiException
from datetime import datetime, timedelta
import ast
import orjson
from tqdm import tqdm
import aiohttp
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from dotenv import load_dotenv
import os
import re


load_dotenv()
api_key = os.getenv('INTRINIO_API_KEY')

directory_path = "json/all-options-contracts"
current_date = datetime.now().date()

async def save_json(data, symbol, contract_id):
    directory_path = f"json/all-options-contracts/{symbol}"
    os.makedirs(directory_path, exist_ok=True)  # Ensure the directory exists
    with open(f"{directory_path}/{contract_id}.json", 'wb') as file:  # Use binary mode for orjson
        file.write(orjson.dumps(data))

def safe_round(value):
    try:
        return round(float(value), 2)
    except (ValueError, TypeError):
        return value

class OptionsResponse:
    @property
    def chain(self):
        return self._chain
        
class ChainItem:
    @property
    def prices(self):
        return self._prices


def calculate_net_premium(ask_price, bid_price, ask_size, bid_size):
    """
    Calculate the net premium from the ask and bid prices and sizes.
    If any value is None, it will be treated as 0.
    """
    # Replace None with 0 for any of the values
    ask_price = ask_price if ask_price is not None else 0
    bid_price = bid_price if bid_price is not None else 0
    ask_size = ask_size if ask_size is not None else 0
    bid_size = bid_size if bid_size is not None else 0
    
    # Premium for call or put options
    ask_premium = ask_price * ask_size * 100  # Assuming 100 shares per contract
    bid_premium = bid_price * bid_size * 100
    
    # Return the net premium (difference between received and paid)
    return ask_premium - bid_premium

intrinio.ApiClient().set_api_key(api_key)
intrinio.ApiClient().allow_retries(True)

after = datetime.today().strftime('%Y-%m-%d')
before = '2100-12-31'
N_year_ago = datetime.now() - timedelta(days=365)
include_related_symbols = False
page_size = 5000
MAX_CONCURRENT_REQUESTS = 100  # Adjust based on API rate limits
BATCH_SIZE = 1500

def get_all_expirations(symbol):
    response = intrinio.OptionsApi().get_options_expirations_eod(
        symbol, 
        after=after, 
        before=before, 
        include_related_symbols=include_related_symbols
    )
    data = (response.__dict__).get('_expirations')
    return data

async def get_options_chain(symbol, expiration, semaphore):
    async with semaphore:
        try:
            # Run the synchronous API call in a thread pool since intrinio doesn't support async
            loop = asyncio.get_event_loop()
            with ThreadPoolExecutor() as pool:
                response = await loop.run_in_executor(
                    pool,
                    lambda: intrinio.OptionsApi().get_options_chain_eod(
                        symbol,
                        expiration,
                        include_related_symbols=include_related_symbols
                    )
                )
            contracts = set()
            for item in response.chain:
                try:
                    contracts.add(item.option.code)
                except Exception as e:
                    print(f"Error processing contract in {expiration}: {e}")
            return contracts
            
        except:
            return set()


async def get_single_contract_eod_data(symbol, contract_id, semaphore):
    url = f"https://api-v2.intrinio.com/options/prices/{contract_id}/eod?api_key={api_key}"

    async with semaphore:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        print(f"Failed to fetch data for {contract_id}: {response.status}")
                        return None

                    response_data = await response.json()
                   

            # Extract and process the response data
            key_data = {k: v for k, v in response_data.get("option", {}).items() if isinstance(v, (str, int, float, bool, list, dict, type(None)))}

            history = []
            if "prices" in response_data:
                for price in response_data["prices"]:
                    history.append({
                        k: v for k, v in price.items() if isinstance(v, (str, int, float, bool, list, dict, type(None)))
                    })

            # Clean the data
            history = [
                {key.lstrip('_'): value for key, value in record.items() if key not in ('close_time', 'open_ask', 'ask_low', 'close_size', 'exercise_style', 'discriminator', 'open_bid', 'bid_low', 'bid_high', 'ask_high')}
                for record in history
            ]

            # Ignore small volume and open interest contracts
            total_volume = sum(item.get('volume', 0) or 0 for item in history)
            total_open_interest = sum(item.get('open_interest', 0) or 0 for item in history)
            count = len(history)
            avg_volume = int(total_volume / count) if count > 0 else 0
            avg_open_interest = int(total_open_interest / count) if count > 0 else 0

            #filter out the trash
            if avg_volume > 10 and avg_open_interest > 10:
                res_list = []

                for item in history:
                    try:
                        new_item = {
                            key: safe_round(value)
                            for key, value in item.items()
                        }
                        res_list.append(new_item)
                    except:
                        pass

                res_list = sorted(res_list, key=lambda x: x['date'])

                for i in range(1, len(res_list)):
                    try:
                        current_open_interest = res_list[i]['open_interest']
                        previous_open_interest = res_list[i-1]['open_interest'] or 0
                        changes_percentage_oi = round((current_open_interest / previous_open_interest - 1) * 100, 2)
                        res_list[i]['changeOI'] = current_open_interest - previous_open_interest
                        res_list[i]['changesPercentageOI'] = changes_percentage_oi
                    except:
                        res_list[i]['changeOI'] = None
                        res_list[i]['changesPercentageOI'] = None

                for i in range(1, len(res_list)):
                    try:
                        volume = res_list[i]['volume']
                        avg_fill = res_list[i]['mark']
                        res_list[i]['gex'] = res_list[i]['gamma'] * res_list[i]['open_interest'] * 100
                        res_list[i]['dex'] = res_list[i]['delta'] * res_list[i]['open_interest'] * 100
                        res_list[i]['total_premium'] = int(avg_fill * volume * 100)
                    except:
                        res_list[i]['total_premium'] = 0

                data = {'expiration': key_data.get('expiration'), 'strike': key_data.get('strike'), 'optionType': key_data.get('type'), 'history': res_list}

                if data:
                    await save_json(data, symbol, contract_id)

        except Exception as e:
            print(f"Error fetching data for {contract_id}: {e}")
            return None


    

async def get_data(symbol, expiration_list):
    # Use a semaphore to limit concurrent requests
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    
    # Create tasks for all expirations
    tasks = [get_options_chain(symbol, expiration, semaphore) for expiration in expiration_list]
    
    # Show progress bar for completed tasks
    contract_sets = set()
    for task in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Processing expirations"):
        contracts = await task
        contract_sets.update(contracts)
    
    # Convert final set to list
    contract_list = list(contract_sets)
    return contract_list


async def process_batch(symbol, batch, semaphore, pbar):
    tasks = [get_single_contract_eod_data(symbol, contract, semaphore) for contract in batch]
    results = []
    
    for task in asyncio.as_completed(tasks):
        result = await task
        if result:
            results.append(result)
        pbar.update(1)
    
    return results

async def process_contracts(symbol, contract_list):
    results = []
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    
    # Calculate total batches for better progress tracking
    total_contracts = len(contract_list)
    total_batches = (total_contracts + BATCH_SIZE - 1) // BATCH_SIZE
    
    with tqdm(total=total_contracts, desc="Processing contracts") as pbar:
        for batch_num in range(total_batches):
            try:
                start_idx = batch_num * BATCH_SIZE
                batch = contract_list[start_idx:start_idx + BATCH_SIZE]
                            
                # Process the batch concurrently
                batch_results = await process_batch(symbol, batch, semaphore, pbar)
                results.extend(batch_results)
            except:
                pass
        
    
    return results

def get_total_symbols():
    with sqlite3.connect('stocks.db') as con:
        cursor = con.cursor()
        cursor.execute("PRAGMA journal_mode = wal")
        cursor.execute("SELECT DISTINCT symbol FROM stocks WHERE symbol NOT LIKE '%.%'")
        stocks_symbols = [row[0] for row in cursor.fetchall()]

    with sqlite3.connect('etf.db') as etf_con:
        etf_cursor = etf_con.cursor()
        etf_cursor.execute("PRAGMA journal_mode = wal")
        etf_cursor.execute("SELECT DISTINCT symbol FROM etfs")
        etf_symbols = [row[0] for row in etf_cursor.fetchall()]

    return stocks_symbols + etf_symbols


def get_expiration_date(option_symbol):
    # Define regex pattern to match the symbol structure
    match = re.match(r"([A-Z]+)(\d{6})([CP])(\d+)", option_symbol)
    if not match:
        raise ValueError(f"Invalid option_symbol format: {option_symbol}")
    
    ticker, expiration, option_type, strike_price = match.groups()
    
    # Convert expiration to datetime
    date_expiration = datetime.strptime(expiration, "%y%m%d").date()
    return date_expiration

def check_contract_expiry(symbol):
    directory = f"{directory_path}/{symbol}/"
    try:
        # Ensure the directory exists
        if not os.path.exists(directory):
            raise FileNotFoundError(f"The directory '{directory}' does not exist.")
        
        # Iterate through all JSON files in the directory
        for file in os.listdir(directory):
            try:
                if file.endswith(".json"):
                    contract_id = file.replace(".json", "")
                    expiration_date = get_expiration_date(contract_id)
                    
                    # Check if the contract is expired
                    if expiration_date < current_date:
                        # Delete the expired contract JSON file
                        os.remove(os.path.join(directory, file))
                        print(f"Deleted expired contract: {contract_id}")
            except:
                pass
        
        # Return the list of non-expired contracts
        return [file.replace(".json", "") for file in os.listdir(directory) if file.endswith(".json")]
    
    except:
        pass

async def process_symbol(symbol):
    try:
        print(f"==========Start Process for {symbol}==========")
        expiration_list = get_all_expirations(symbol)
        #check existing contracts and delete expired ones
        check_contract_expiry(symbol)

        print(f"Found {len(expiration_list)} expiration dates")
        contract_list = await get_data(symbol, expiration_list)
        print(f"Unique contracts: {len(contract_list)}")

        if len(contract_list) > 0:
            results = await process_contracts(symbol, contract_list)
    except:
        pass


def get_tickers_from_directory(directory: str):
    if not os.path.isdir(directory):
        print(f"Error: '{directory}' is not a valid directory.")
        return []
    try:
        return [
            folder 
            for folder in os.listdir(directory) 
            if os.path.isdir(os.path.join(directory, folder))
        ]
    except Exception as e:
        print(f"An error occurred while accessing '{directory}': {e}")
        return []

async def main():
    total_symbols = get_tickers_from_directory(directory_path)
    if len(total_symbols) < 2000:
        total_symbols = get_total_symbols()
    
    for symbol in tqdm(total_symbols):
        await process_symbol(symbol)

if __name__ == "__main__":
    asyncio.run(main())
