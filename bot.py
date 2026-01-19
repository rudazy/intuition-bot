import os
import logging
import sqlite3
import asyncio
import discord
import aiohttp
from discord.ext import commands
from dotenv import load_dotenv
from urllib.parse import urlparse

# Configuration
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
GRAPHQL_URL = os.getenv('INTUITION_GRAPHQL_URL', 'https://mainnet.intuition.sh/v1/graphql')
DATABASE_URL = os.getenv('DATABASE_URL')

# Logging setup for Railway
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Discord setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Database connection pool (PostgreSQL)
pg_pool = None


class Database:
    """Database abstraction supporting both SQLite and PostgreSQL."""
    
    def __init__(self):
        self.use_postgres = DATABASE_URL is not None
        self.sqlite_path = 'intuition_registry.db'
    
    async def init(self):
        """Initialize database connection and create tables."""
        if self.use_postgres:
            await self._init_postgres()
        else:
            self._init_sqlite()
    
    async def _init_postgres(self):
        """Initialize PostgreSQL connection pool."""
        global pg_pool
        try:
            import asyncpg
            pg_pool = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=2,
                max_size=10,
                command_timeout=30
            )
            async with pg_pool.acquire() as conn:
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS registry (
                        nickname TEXT PRIMARY KEY,
                        wallet TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
            logger.info('PostgreSQL database initialized')
        except Exception as e:
            logger.error(f'PostgreSQL initialization failed: {e}')
            raise
    
    def _init_sqlite(self):
        """Initialize SQLite database."""
        try:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS registry (
                    nickname TEXT PRIMARY KEY,
                    wallet TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
            conn.close()
            logger.info('SQLite database initialized')
        except Exception as e:
            logger.error(f'SQLite initialization failed: {e}')
            raise
    
    async def get_wallet(self, nickname: str) -> str:
        """Get wallet address for a nickname."""
        if self.use_postgres:
            async with pg_pool.acquire() as conn:
                row = await conn.fetchrow(
                    'SELECT wallet FROM registry WHERE nickname = $1',
                    nickname
                )
                return row['wallet'] if row else None
        else:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()
            cursor.execute('SELECT wallet FROM registry WHERE nickname = ?', (nickname,))
            row = cursor.fetchone()
            conn.close()
            return row[0] if row else None
    
    async def link_wallet(self, nickname: str, wallet: str) -> bool:
        """Link a wallet to a nickname."""
        try:
            if self.use_postgres:
                async with pg_pool.acquire() as conn:
                    await conn.execute('''
                        INSERT INTO registry (nickname, wallet)
                        VALUES ($1, $2)
                        ON CONFLICT (nickname) DO UPDATE SET wallet = $2
                    ''', nickname, wallet)
            else:
                conn = sqlite3.connect(self.sqlite_path)
                cursor = conn.cursor()
                cursor.execute(
                    'INSERT OR REPLACE INTO registry (nickname, wallet) VALUES (?, ?)',
                    (nickname, wallet)
                )
                conn.commit()
                conn.close()
            return True
        except Exception as e:
            logger.error(f'Failed to link wallet: {e}')
            return False
    
    async def unlink_wallet(self, nickname: str) -> bool:
        """Remove a nickname from the registry."""
        try:
            if self.use_postgres:
                async with pg_pool.acquire() as conn:
                    result = await conn.execute(
                        'DELETE FROM registry WHERE nickname = $1',
                        nickname
                    )
                    return 'DELETE 1' in result
            else:
                conn = sqlite3.connect(self.sqlite_path)
                cursor = conn.cursor()
                cursor.execute('DELETE FROM registry WHERE nickname = ?', (nickname,))
                affected = cursor.rowcount
                conn.commit()
                conn.close()
                return affected > 0
        except Exception as e:
            logger.error(f'Failed to unlink wallet: {e}')
            return False
    
    async def close(self):
        """Close database connections."""
        if self.use_postgres and pg_pool:
            await pg_pool.close()
            logger.info('PostgreSQL connection pool closed')


# Initialize database instance
db = Database()


def is_valid_address(address: str) -> bool:
    """Validate Ethereum address format."""
    if not address:
        return False
    if not address.startswith('0x'):
        return False
    if len(address) != 42:
        return False
    try:
        int(address, 16)
        return True
    except ValueError:
        return False


async def fetch_intuition_stats(address: str) -> dict:
    """Fetch user stats from Intuition GraphQL API."""
    addr = address.lower()
    result = {
        'label': None,
        'staked': 0.0,
        'activity': 0,
        'utilization': '0%'
    }

    timeout = aiohttp.ClientTimeout(total=30)
    
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Query 1: Get identity label
        try:
            query_label = '''
            query GetLabel($address: String!) {
                atoms(where: {data: {_ilike: $address}}) {
                    label
                }
            }
            '''
            async with session.post(
                GRAPHQL_URL,
                json={'query': query_label, 'variables': {'address': addr}},
                headers={'Content-Type': 'application/json'}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    atoms = data.get('data', {}).get('atoms', [])
                    if atoms:
                        result['label'] = atoms[0].get('label')
        except Exception as e:
            logger.warning(f'Failed to fetch label for {addr}: {e}')

        # Query 2: Get network activity
        try:
            query_activity = '''
            query GetActivity($address: String!) {
                accounts(where: {id: {_ilike: $address}}) {
                    triples_aggregate {
                        aggregate {
                            count
                        }
                    }
                    deposits_sent_aggregate {
                        aggregate {
                            count
                        }
                    }
                }
            }
            '''
            async with session.post(
                GRAPHQL_URL,
                json={'query': query_activity, 'variables': {'address': addr}},
                headers={'Content-Type': 'application/json'}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    accounts = data.get('data', {}).get('accounts', [])
                    if accounts:
                        acc = accounts[0]
                        triples = acc.get('triples_aggregate', {}).get('aggregate', {}).get('count', 0)
                        deposits = acc.get('deposits_sent_aggregate', {}).get('aggregate', {}).get('count', 0)
                        result['activity'] = triples + deposits
        except Exception as e:
            logger.warning(f'Failed to fetch activity for {addr}: {e}')

        # Query 3: Get TRUST staked (paginated)
        try:
            total_trust_wei = 0
            offset = 0
            limit = 50

            while True:
                query_positions = '''
                query GetPositions($address: String!, $limit: Int!, $offset: Int!) {
                    positions(
                        limit: $limit,
                        offset: $offset,
                        where: {account_id: {_ilike: $address}}
                    ) {
                        shares
                    }
                }
                '''
                async with session.post(
                    GRAPHQL_URL,
                    json={
                        'query': query_positions,
                        'variables': {'address': addr, 'limit': limit, 'offset': offset}
                    },
                    headers={'Content-Type': 'application/json'}
                ) as resp:
                    if resp.status != 200:
                        break
                    data = await resp.json()
                    positions = data.get('data', {}).get('positions', [])
                    
                    if not positions:
                        break

                    for pos in positions:
                        shares = pos.get('shares') or pos.get('total_redeem_assets_for_receiver') or 0
                        total_trust_wei += float(shares)

                    offset += limit
                    if offset > 2000:
                        break

            result['staked'] = total_trust_wei / 1e18
            
            if result['staked'] > 100:
                result['utilization'] = '90%'
            elif result['staked'] > 10:
                result['utilization'] = '50%'
            elif result['staked'] > 0:
                result['utilization'] = '15%'
                
        except Exception as e:
            logger.warning(f'Failed to fetch positions for {addr}: {e}')

    return result


@bot.event
async def on_ready():
    """Called when bot is ready and connected to Discord."""
    await db.init()
    logger.info(f'Bot connected as {bot.user} (ID: {bot.user.id})')
    logger.info(f'Connected to {len(bot.guilds)} guild(s)')
    logger.info(f'Database: {"PostgreSQL" if db.use_postgres else "SQLite"}')


@bot.event
async def on_command_error(ctx, error):
    """Global error handler for commands."""
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f'Missing argument: {error.param.name}')
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        logger.error(f'Command error: {error}')
        await ctx.send('An error occurred while processing your request.')


@bot.command(name='link')
async def link_wallet(ctx, wallet: str, nickname: str):
    """Link a wallet address to a nickname."""
    if not is_valid_address(wallet):
        await ctx.send('Invalid wallet address. Must be a valid Ethereum address.')
        return

    nickname_clean = nickname.lower().strip()
    if len(nickname_clean) < 2 or len(nickname_clean) > 32:
        await ctx.send('Nickname must be between 2 and 32 characters.')
        return

    success = await db.link_wallet(nickname_clean, wallet.lower())
    
    if success:
        logger.info(f'Linked {nickname_clean} to {wallet[:10]}...')
        await ctx.send(f'Linked **{nickname_clean}** to wallet `{wallet[:6]}...{wallet[-4:]}`')
    else:
        await ctx.send('Failed to save link. Please try again.')


@bot.command(name='rep')
async def reputation(ctx, identifier: str = None):
    """Fetch Intuition reputation for a nickname or wallet."""
    if not identifier:
        await ctx.send('Usage: `!rep <nickname>` or `!rep <wallet_address>`')
        return

    identifier_clean = identifier.lower().strip()
    wallet = None

    # Check if it's a direct wallet address
    if is_valid_address(identifier_clean):
        wallet = identifier_clean
    else:
        # Look up nickname in registry
        wallet = await db.get_wallet(identifier_clean)
        if not wallet:
            await ctx.send(f'Nickname **{identifier_clean}** not found. Use `!link <wallet> <nickname>` first.')
            return

    # Fetch stats from Intuition
    msg = await ctx.send(f'Fetching Intuition data for **{identifier_clean}**...')
    
    try:
        stats = await fetch_intuition_stats(wallet)

        embed = discord.Embed(
            title=f'Intuition Profile: {identifier_clean}',
            color=0x5865F2
        )
        embed.set_author(name='Intuition Network')

        display_label = stats['label'] if stats['label'] else f'0x{wallet[2]}...{wallet[-2:]}'
        
        embed.add_field(name='Identity', value=display_label, inline=True)
        embed.add_field(name='Utilization', value=stats['utilization'], inline=True)
        embed.add_field(name='Network Activity', value=f"{stats['activity']} Actions", inline=True)
        embed.add_field(name='TRUST Staked', value=f"**{stats['staked']:,.2f}**", inline=False)
        embed.set_footer(text='Intuition Mainnet')

        await msg.edit(content=None, embed=embed)
        logger.info(f'Fetched rep for {identifier_clean}: {stats["staked"]:.2f} TRUST')

    except Exception as e:
        logger.error(f'Failed to fetch stats for {wallet}: {e}')
        await msg.edit(content='Failed to fetch Intuition data. Please try again later.')


@bot.command(name='link')
async def link_wallet(ctx, wallet: str, nickname: str):
    """Link a wallet address to a nickname."""
    # Delete the user's command message for privacy
    try:
        await ctx.message.delete()
    except:
        pass

    if not is_valid_address(wallet):
        msg = await ctx.send('Invalid wallet address.')
        await asyncio.sleep(3)
        await msg.delete()
        return

    nickname_clean = nickname.lower().strip()
    if len(nickname_clean) < 2 or len(nickname_clean) > 32:
        msg = await ctx.send('Nickname must be between 2 and 32 characters.')
        await asyncio.sleep(3)
        await msg.delete()
        return

    success = await db.link_wallet(nickname_clean, wallet.lower())
    
    if success:
        logger.info(f'Linked {nickname_clean} to 0x{wallet[2]}...{wallet[-2:]}')
        msg = await ctx.send(f'Linked **{nickname_clean}** successfully.')
        await asyncio.sleep(3)
        await msg.delete()
    else:
        msg = await ctx.send('Failed to save link. Please try again.')
        await asyncio.sleep(3)
        await msg.delete()


@bot.command(name='stats')
async def bot_stats(ctx):
    """Show bot statistics."""
    embed = discord.Embed(
        title='Intuition Rep Bot',
        color=0x5865F2
    )
    embed.add_field(name='Servers', value=str(len(bot.guilds)), inline=True)
    embed.add_field(name='Database', value='PostgreSQL' if db.use_postgres else 'SQLite', inline=True)
    embed.add_field(name='GraphQL', value=GRAPHQL_URL.split('//')[-1].split('/')[0], inline=True)
    embed.set_footer(text='Intuition Network')
    await ctx.send(embed=embed)


def main():
    """Main entry point."""
    if not TOKEN:
        logger.error('DISCORD_TOKEN not set in environment variables')
        return

    logger.info('Starting Intuition Rep Bot...')
    logger.info(f'GraphQL Endpoint: {GRAPHQL_URL}')
    logger.info(f'Database Mode: {"PostgreSQL" if DATABASE_URL else "SQLite"}')
    
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        logger.error('Invalid Discord token')
    except KeyboardInterrupt:
        logger.info('Bot stopped by user')
    except Exception as e:
        logger.error(f'Bot crashed: {e}')


if __name__ == '__main__':
    main()