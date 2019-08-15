import os

# Account used to sign transactions with fees
SIGNER_ACCOUNT = int(os.getenv('SIGNER_ACCOUNT', 123456))
DONATION_ACCOUNT = int(os.getenv('DONATION_ACCOUNT', 1185729))
# Public key of our account
PUBKEY_B58 = os.getenv('PUBKEY_B58', '123456')
# Soft Expiry of Borrowed Pasa (milliseconds)
PASA_SOFT_EXPIRY = 259200000 # 3 days
# Hard expiry of borrowed pasa (seconds)
PASA_HARD_EXPIRY = 2592000 # 30 days
# Price of borrowed accounts
PASA_PRICE = 0.25
# Limit of borrowed accounts
PASA_LIMIT = 2