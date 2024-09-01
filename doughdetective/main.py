import requests
import configparser
import requests_cache
import urllib3
import argparse
import calendar
import json
import csv
from typing import Optional
from datetime import datetime

# Ignore warnings when using self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

## Read configuration
config = configparser.ConfigParser()
config.read('config.ini')
general_config = config['GENERAL']
base_url = general_config['server']
api_token = general_config['token']

requests_cache.install_cache('firefly_cache', expire_after=60)

def load_config():
    with open('config.json', 'r') as file:
        config = json.load(file)
    return config

def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'date', type=str,help='Date in YYYYMM format'
    )
    parser.add_argument(
        '-a','--account', type=str, required=True, help='Name of your account'
    )
    return parser.parse_args()

def pretty(dictionary):
    return(json.dumps(dictionary, indent=2))

def read_csv(csv_file, config):
    with open(csv_file, 'r') as f:
        transactions = []
        reader = csv.reader(f, delimiter=',')
        if config['has_header'] == "True":
            next(reader)  # skip header
        for row in reader:
            # Exclude ribo payment
            if config['has_ribo'] == "True":
                if '現地利用額' in row[1]: # Exclude rows that have conversions　
                    continue
                if 'リボ' in row[1] or 'リボ' in row[3]: # Exclude ribo charges
                    continue

            formatted_date = datetime.strptime(
                row[config['date_column']],
                config['date_format']).strftime('%Y/%m/%d')
            
            transactions.append({
                'date': formatted_date,
                'name': row[config['description_column']],
                'amount': row[config['amount_column']]
            })
    return transactions

def get_first_and_last_day(year, month):
    if month < 1 or month > 12:
        raise ValueError("Month must be between 1 and 12")
    first_day = datetime(year, month, 1)
    _, last_day = calendar.monthrange(year, month)
    last_day_date = datetime(year, month, last_day)
    return first_day, last_day_date
   
def compare_transactions(csv_transactions, firefly_transactions):
    print('###Transactions missing in FF###')
    for csv_txn in csv_transactions:
        matching_txn_found = False
        for ff_txn in firefly_transactions:
            if csv_txn['date'] == ff_txn['date'] and csv_txn['amount'] == ff_txn['amount']:
                matching_txn_found = True
                break
        if not matching_txn_found:
            print(
                f'Date: {csv_txn['date']}, '
                f'Amount: {csv_txn['amount']}, '
                f'Description: {csv_txn['name']}'
            )
    
    print('###Transactions missing in CSV###')
    for ff_txn in firefly_transactions:
        
        matching_txn_found = False
        for csv_txn in csv_transactions:            
            if csv_txn['date'] == ff_txn['date'] and csv_txn['amount'] == ff_txn['amount']:
                matching_txn_found = True
                break
        if not matching_txn_found:
            print(f"Date: {ff_txn['date']}, "
                  f"Amount: {ff_txn['amount']}, "
                  f"Description: {ff_txn['name']}")


class FireflyAPIClient:
    def __init__(self, base_url, api_token):
        self.base_url = base_url.rstrip('/') # Remove trailing slash if present
        self.api_token = api_token
        self.page = 1
    def make_api_call(self, endpoint, params=None):
        page = 1
        headers = {
            'Authorization': f'Bearer {self.api_token}',
            'Accept': 'application/json'
        }
        all_data = []
        while True:
            params = params or {}
            params['page'] = page
            response = requests.get(
                f'{self.base_url}/{endpoint}', params=params, headers=headers, 
                verify=False
            )
            # Raise an exception for 4xx or 5xx status codes
            response.raise_for_status()
            data = response.json()
            all_data.extend(data['data'])
            page += 1
            if page > data['meta']['pagination']['total_pages']:
                break
        return all_data

    def get_account_transactions(self, start_date, end_date, account_id=None):
        out = []
        transactions = self.make_api_call(
                'transactions', {'start': start_date, 'end': end_date}
        )
        if account_id:
            for transaction in transactions:
                if transaction["attributes"]["transactions"][0]["source_id"] == account_id: 
                    out.append(transaction)
        else:
            out = transactions
        return out

    def get_account_id(self, account_name: str) -> Optional[str]:
        accounts = self.make_api_call('accounts')
        for account in accounts:
            attributes = account['attributes']
            if attributes['name'] == account_name:
                return account['id']
        return None
    def get_transaction_list(self, start_date, end_date, account_id):
        '''
        Get a formatted list of transactions for given dates and account id. 
        Used in conjunction with comparing to a csv
        '''
        firefly_transactions = []
        for transaction in self.get_account_transactions(start_date, end_date, account_id):
            # Catch split transactions
            if trans_name := transaction['attributes']['group_title']:
                total_cost = 0
                # Calculate total transaction cost
                for sub_trans in transaction['attributes']['transactions']:
                    total_cost = total_cost +int(sub_trans['amount'].split('.')[0])
                trans_amount = str(total_cost)
            else:
                # Assume the transaction is not a split-transaction
                trans_name = transaction['attributes']['transactions'][0]['description']
                trans_amount = transaction['attributes']['transactions'][0]['amount'].split('.')[0]
            
            # Get the first transaction's date. 
            # Assume even split-transactions are made on the same day 
            trans_date = transaction['attributes']['transactions'][0]['date']
            formatted_date = datetime.strptime(trans_date, '%Y-%m-%dT%H:%M:%S+00:00').strftime('%Y/%m/%d')
            firefly_transactions.append({
                'date': formatted_date,
                'name': trans_name,
                'amount': trans_amount
            })
        return firefly_transactions



def main():
    args = parse_arguments()
    date_str = args.date
    account_name = args.account
    config = load_config()
    if len(date_str) != 6 or not date_str.isdigit():
        print("Date must be in YYYYMM format (e.g., 202409 for September 2024).")
        return
    month = int(date_str[4:])
    year = int(date_str[:4])

    first_day, last_day = get_first_and_last_day(year, month)
    format_config = config['formats'][account_name]
    fc = FireflyAPIClient(base_url, api_token)
    account_id = fc.get_account_id(format_config["ff_account_id_name"])
    firefly_transactions = fc.get_transaction_list(
            first_day.strftime('%Y-%m-%d'),
            last_day.strftime('%Y-%m-%d'),
            account_id
    )
    csv_transactions = read_csv(
        f'csv_files/{date_str}_{account_name}.csv', format_config
    )
    compare_transactions(csv_transactions, firefly_transactions)


if __name__ == "__main__":
    main()
