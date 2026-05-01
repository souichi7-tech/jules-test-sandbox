import asyncio
import json
import logging
import os
import ssl
from typing import Optional

import websockets
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from solders.signature import Signature

# Load environment variables
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SOLANA_WSS_URL = os.getenv("SOLANA_WSS_URL", "wss://api.mainnet-beta.solana.com")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

# Raydium AMM V4 Program ID
RAYDIUM_PROGRAM_ID = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

async def send_telegram_message(bot: Bot, text: str):
    try:
        from telegram import LinkPreviewOptions
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")

async def fetch_transaction_details(signature: str) -> Optional[dict]:
    """Fetch transaction details to extract token addresses."""
    # We use a short timeout and specific commitment
    async with AsyncClient(SOLANA_RPC_URL) as client:
        try:
            # Need to use max_supported_transaction_version for versioned transactions
            sig_obj = Signature.from_string(signature)
            response = await client.get_transaction(
                sig_obj,
                encoding="jsonParsed",
                max_supported_transaction_version=0
            )
            return response
        except Exception as e:
            logger.error(f"Error fetching transaction {signature}: {e}")
            return None

def extract_tokens_from_transaction(tx_response) -> Optional[tuple[str, str]]:
    """Extract Base and Quote token addresses from Raydium InitializeInstruction2."""
    if not tx_response or not tx_response.value:
        return None

    try:
        transaction = tx_response.value.transaction
        meta = tx_response.value.meta

        if meta and meta.err is not None:
            # Transaction failed
            return None

        # Usually, the preTokenBalances or postTokenBalances contain the mints
        # Or we can look at the account keys. For Raydium InitializeInstruction2,
        # Account 8 is usually the coin mint, Account 9 is pc mint (base/quote)

        # A simpler robust way: find the Raydium instruction, then extract from its accounts
        message = transaction.message

        # message can be UiRawMessage or UiParsedMessage depending on encoding
        if hasattr(message, "account_keys"):
            if hasattr(message.account_keys[0], "pubkey"):
                account_keys = [str(acc.pubkey) for acc in message.account_keys]
            else:
                account_keys = [str(acc) for acc in message.account_keys]
        else:
            return None

        for instruction in message.instructions:
            if str(instruction.program_id) == RAYDIUM_PROGRAM_ID:
                # We can't easily parse the instruction data without borsh, but we can assume
                # based on the number of accounts (InitializeInstruction2 has ~17 accounts)
                # Usually:
                # 8: coin_mint
                # 9: pc_mint
                if hasattr(instruction, 'accounts') and len(instruction.accounts) >= 17:
                    coin_mint = str(instruction.accounts[8])
                    pc_mint = str(instruction.accounts[9])
                    return coin_mint, pc_mint

        # Fallback: Check token balances
        mints = set()
        if meta.pre_token_balances:
            for balance in meta.pre_token_balances:
                mints.add(balance.mint)
        if meta.post_token_balances:
            for balance in meta.post_token_balances:
                mints.add(balance.mint)

        # Remove WSOL if present, typically WSOL is the quote token
        wsol = "So11111111111111111111111111111111111111112"
        other_mints = [m for m in mints if m != wsol]

        if other_mints and wsol in mints:
            return other_mints[0], wsol
        elif len(mints) >= 2:
            mints_list = list(mints)
            return mints_list[0], mints_list[1]

    except Exception as e:
        logger.error(f"Error parsing transaction: {e}")

    return None

async def monitor_raydium(bot: Bot):
    """Subscribe to Solana WebSocket and listen for Raydium pool creation."""
    logger.info(f"Starting Raydium monitor on {SOLANA_WSS_URL}...")

    # Payload to subscribe to logs mentioning the Raydium program
    subscribe_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "logsSubscribe",
        "params": [
            {"mentions": [RAYDIUM_PROGRAM_ID]},
            {"commitment": "confirmed"}
        ]
    }

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    while True:
        try:
            async with websockets.connect(SOLANA_WSS_URL, ping_interval=20, ping_timeout=20, ssl=ssl_context if SOLANA_WSS_URL.startswith('wss') else None) as websocket:
                await websocket.send(json.dumps(subscribe_request))
                logger.info("Subscribed to Raydium logs.")

                while True:
                    response = await websocket.recv()
                    data = json.loads(response)

                    if "params" in data and "result" in data["params"]:
                        result = data["params"]["result"]
                        value = result.get("value", {})
                        logs = value.get("logs", [])
                        signature = value.get("signature")

                        if not logs or not signature:
                            continue

                        # Check if this log indicates pool initialization
                        is_initialization = any(
                            "Instruction: InitializeInstruction2" in log or
                            "Instruction: Initialize" in log
                            for log in logs
                        )

                        if is_initialization:
                            logger.info(f"New pool initialization detected! Signature: {signature}")

                            # Wait briefly to ensure transaction is available via HTTP RPC
                            await asyncio.sleep(2)

                            tx_data = await fetch_transaction_details(signature)
                            if tx_data:
                                tokens = extract_tokens_from_transaction(tx_data)
                                if tokens:
                                    token1, token2 = tokens

                                    # Identify base token (assuming one is SOL)
                                    wsol = "So11111111111111111111111111111111111111112"
                                    if token2 == wsol:
                                        base_token = token1
                                    elif token1 == wsol:
                                        base_token = token2
                                    else:
                                        base_token = token1 # Default

                                    message = (
                                        f"🚀 <b>New Raydium Pool Detected!</b>\n\n"
                                        f"<b>Token Address:</b>\n"
                                        f"<code>{base_token}</code>\n\n"
                                        f"<b>Transaction:</b>\n"
                                        f"<a href='https://solscan.io/tx/{signature}'>View on Solscan</a>"
                                    )
                                    await send_telegram_message(bot, message)
                                else:
                                    logger.warning(f"Could not extract tokens for tx {signature}")
        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket connection closed. Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"WebSocket error: {e}. Reconnecting in 5 seconds...")
            await asyncio.sleep(5)

async def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env file.")
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    # Test connection
    try:
        me = await bot.get_me()
        logger.info(f"Bot authenticated as @{me.username}")
    except Exception as e:
        logger.error(f"Failed to authenticate bot: {e}")
        return

    # Start monitoring task
    await monitor_raydium(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped manually.")
