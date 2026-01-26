import os
import logging
import sqlite3
import asyncio
import discord
import aiohttp
import json
from datetime import datetime, timezone
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from typing import Optional, Dict, List, Any

# Configuration
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
GRAPHQL_URL = os.getenv('INTUITION_GRAPHQL_URL', 'https://mainnet.intuition.sh/v1/graphql')
DATABASE_URL = os.getenv('DATABASE_URL')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Discord setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

# PostgreSQL connection pool
pg_pool = None

# =============================================================================
# TRUST SCORING CONFIGURATION (from Intuition MCP)
# =============================================================================

# Predicate weights for trust scoring
PREDICATE_WEIGHTS = {
    # High trust predicates (1.5x)
    'high': {
        'is trusted by',
        'is verified by',
        'is endorsed by',
        'is vouched for by',
        'is recommended by',
        'has collaborated with',
        'is a core contributor to',
        'is a builder of',
    },
    # Medium trust predicates (1.0x)
    'medium': {
        'is known by',
        'is followed by',
        'is a member of',
        'has worked with',
        'is affiliated with',
        'is connected to',
        'is associated with',
    },
    # Low trust predicates (0.5x)
    'low': {
        'is aware of',
        'has met',
        'is interested in',
        'has interacted with',
    },
    # Negative predicates (-2.0x)
    'negative': {
        'is distrusted by',
        'is flagged by',
        'is reported by',
        'is blocked by',
        'is scam',
        'is suspicious',
        'is fake',
    }
}

WEIGHT_MULTIPLIERS = {
    'high': 1.5,
    'medium': 1.0,
    'low': 0.5,
    'negative': -2.0
}

# Transitive trust decay per hop
TRANSITIVE_DECAY = [1.0, 0.5, 0.25]  # Hop 1, 2, 3

# Time decay configuration (attestations older than this decay)
TIME_DECAY_DAYS = 365
TIME_DECAY_FACTOR = 0.5  # 50% weight for old attestations


# =============================================================================
# DATABASE
# =============================================================================

class Database:
    """Database abstraction supporting both SQLite and PostgreSQL."""

    def __init__(self):
        self.use_postgres = DATABASE_URL is not None
        self.sqlite_path = 'intuition_users.db'

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
                    CREATE TABLE IF NOT EXISTS user_wallets (
                        discord_id BIGINT PRIMARY KEY,
                        wallet TEXT NOT NULL,
                        linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                CREATE TABLE IF NOT EXISTS user_wallets (
                    discord_id INTEGER PRIMARY KEY,
                    wallet TEXT NOT NULL,
                    linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
            conn.close()
            logger.info('SQLite database initialized')
        except Exception as e:
            logger.error(f'SQLite initialization failed: {e}')
            raise

    async def get_wallet_by_discord_id(self, discord_id: int) -> Optional[str]:
        """Get wallet address for a Discord user ID."""
        if self.use_postgres:
            async with pg_pool.acquire() as conn:
                row = await conn.fetchrow(
                    'SELECT wallet FROM user_wallets WHERE discord_id = $1',
                    discord_id
                )
                return row['wallet'] if row else None
        else:
            conn = sqlite3.connect(self.sqlite_path)
            cursor = conn.cursor()
            cursor.execute('SELECT wallet FROM user_wallets WHERE discord_id = ?', (discord_id,))
            row = cursor.fetchone()
            conn.close()
            return row[0] if row else None

    async def link_wallet(self, discord_id: int, wallet: str) -> bool:
        """Link a wallet to a Discord user ID."""
        try:
            if self.use_postgres:
                async with pg_pool.acquire() as conn:
                    await conn.execute('''
                        INSERT INTO user_wallets (discord_id, wallet)
                        VALUES ($1, $2)
                        ON CONFLICT (discord_id) DO UPDATE SET wallet = $2, linked_at = CURRENT_TIMESTAMP
                    ''', discord_id, wallet)
            else:
                conn = sqlite3.connect(self.sqlite_path)
                cursor = conn.cursor()
                cursor.execute(
                    'INSERT OR REPLACE INTO user_wallets (discord_id, wallet, linked_at) VALUES (?, ?, datetime("now"))',
                    (discord_id, wallet)
                )
                conn.commit()
                conn.close()
            return True
        except Exception as e:
            logger.error(f'Failed to link wallet: {e}')
            return False

    async def unlink_wallet(self, discord_id: int) -> bool:
        """Remove wallet link for a Discord user."""
        try:
            if self.use_postgres:
                async with pg_pool.acquire() as conn:
                    result = await conn.execute(
                        'DELETE FROM user_wallets WHERE discord_id = $1',
                        discord_id
                    )
                    return 'DELETE 1' in result
            else:
                conn = sqlite3.connect(self.sqlite_path)
                cursor = conn.cursor()
                cursor.execute('DELETE FROM user_wallets WHERE discord_id = ?', (discord_id,))
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


# =============================================================================
# VALIDATION HELPERS
# =============================================================================

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


def get_predicate_weight(predicate: str) -> float:
    """Get the weight multiplier for a predicate."""
    predicate_lower = predicate.lower()
    for category, predicates in PREDICATE_WEIGHTS.items():
        for p in predicates:
            if p in predicate_lower or predicate_lower in p:
                return WEIGHT_MULTIPLIERS[category]
    return WEIGHT_MULTIPLIERS['medium']  # Default to medium


def calculate_time_decay(timestamp: str) -> float:
    """Calculate time decay factor for an attestation."""
    try:
        # Parse timestamp (handle various formats)
        if 'T' in timestamp:
            att_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        else:
            att_time = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)

        now = datetime.now(timezone.utc)
        days_old = (now - att_time).days

        if days_old > TIME_DECAY_DAYS:
            return TIME_DECAY_FACTOR
        return 1.0
    except Exception:
        return 1.0


# =============================================================================
# INTUITION GRAPHQL QUERIES
# =============================================================================

async def fetch_comprehensive_data(address: str) -> Dict[str, Any]:
    """Fetch comprehensive Intuition data for trust scoring and LLM summary."""
    addr = address.lower()
    result = {
        'address': addr,
        'label': None,
        'identity': None,
        'staked': 0.0,
        'activity_count': 0,
        'attestations_received': [],
        'attestations_given': [],
        'vouches': [],
        'positions': [],
        'trust_score': 0.0,
        'categories': [],
        'first_activity': None,
        'verified_builder': False,
    }

    timeout = aiohttp.ClientTimeout(total=45)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Query 1: Identity and label
        try:
            query_identity = '''
            query GetIdentity($address: String!) {
                atoms(where: {data: {_ilike: $address}}) {
                    id
                    label
                    type
                    created_at
                    creator {
                        id
                        label
                    }
                }
            }
            '''
            async with session.post(
                GRAPHQL_URL,
                json={'query': query_identity, 'variables': {'address': addr}},
                headers={'Content-Type': 'application/json'}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    atoms = data.get('data', {}).get('atoms', [])
                    if atoms:
                        result['identity'] = atoms[0]
                        result['label'] = atoms[0].get('label')
                        result['first_activity'] = atoms[0].get('created_at')
        except Exception as e:
            logger.warning(f'Failed to fetch identity for {addr}: {e}')

        # Query 2: Attestations received (triples where this address is the subject)
        try:
            query_attestations = '''
            query GetAttestationsReceived($address: String!) {
                triples(
                    limit: 100,
                    where: {
                        subject: {data: {_ilike: $address}}
                    }
                ) {
                    id
                    predicate {
                        id
                        label
                    }
                    object {
                        id
                        label
                        data
                    }
                    creator {
                        id
                        label
                    }
                    counter_vault {
                        total_shares
                    }
                    vault {
                        total_shares
                    }
                    created_at
                }
            }
            '''
            async with session.post(
                GRAPHQL_URL,
                json={'query': query_attestations, 'variables': {'address': addr}},
                headers={'Content-Type': 'application/json'}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result['attestations_received'] = data.get('data', {}).get('triples', [])
        except Exception as e:
            logger.warning(f'Failed to fetch attestations for {addr}: {e}')

        # Query 3: Attestations given (triples created by this address)
        try:
            query_given = '''
            query GetAttestationsGiven($address: String!) {
                triples(
                    limit: 100,
                    where: {
                        creator: {id: {_ilike: $address}}
                    }
                ) {
                    id
                    subject {
                        id
                        label
                    }
                    predicate {
                        id
                        label
                    }
                    object {
                        id
                        label
                    }
                    created_at
                }
            }
            '''
            async with session.post(
                GRAPHQL_URL,
                json={'query': query_given, 'variables': {'address': addr}},
                headers={'Content-Type': 'application/json'}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result['attestations_given'] = data.get('data', {}).get('triples', [])
        except Exception as e:
            logger.warning(f'Failed to fetch given attestations for {addr}: {e}')

        # Query 4: Account activity and positions
        try:
            query_account = '''
            query GetAccount($address: String!) {
                accounts(where: {id: {_ilike: $address}}) {
                    id
                    label
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
                    positions(limit: 100) {
                        id
                        shares
                        vault {
                            id
                            total_shares
                            atom {
                                id
                                label
                            }
                            triple {
                                id
                                subject {
                                    label
                                }
                                predicate {
                                    label
                                }
                                object {
                                    label
                                }
                            }
                        }
                    }
                }
            }
            '''
            async with session.post(
                GRAPHQL_URL,
                json={'query': query_account, 'variables': {'address': addr}},
                headers={'Content-Type': 'application/json'}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    accounts = data.get('data', {}).get('accounts', [])
                    if accounts:
                        acc = accounts[0]
                        triples = acc.get('triples_aggregate', {}).get('aggregate', {}).get('count', 0)
                        deposits = acc.get('deposits_sent_aggregate', {}).get('aggregate', {}).get('count', 0)
                        result['activity_count'] = triples + deposits
                        result['positions'] = acc.get('positions', [])

                        # Calculate total staked
                        total_shares = sum(
                            float(p.get('shares', 0))
                            for p in result['positions']
                        )
                        result['staked'] = total_shares / 1e18
        except Exception as e:
            logger.warning(f'Failed to fetch account for {addr}: {e}')

        # Query 5: Check for vouches and builder status
        try:
            query_vouches = '''
            query GetVouches($address: String!) {
                triples(
                    limit: 50,
                    where: {
                        _and: [
                            {subject: {data: {_ilike: $address}}},
                            {predicate: {label: {_ilike: "%vouch%"}}}
                        ]
                    }
                ) {
                    id
                    predicate {
                        label
                    }
                    object {
                        label
                        data
                    }
                    creator {
                        id
                        label
                    }
                    vault {
                        total_shares
                    }
                    created_at
                }
            }
            '''
            async with session.post(
                GRAPHQL_URL,
                json={'query': query_vouches, 'variables': {'address': addr}},
                headers={'Content-Type': 'application/json'}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result['vouches'] = data.get('data', {}).get('triples', [])
        except Exception as e:
            logger.warning(f'Failed to fetch vouches for {addr}: {e}')

        # Query 6: Check for builder/verified status
        try:
            query_builder = '''
            query GetBuilderStatus($address: String!) {
                triples(
                    where: {
                        _and: [
                            {subject: {data: {_ilike: $address}}},
                            {_or: [
                                {predicate: {label: {_ilike: "%builder%"}}},
                                {predicate: {label: {_ilike: "%verified%"}}},
                                {object: {label: {_ilike: "%builder%"}}},
                                {object: {label: {_ilike: "%verified%"}}}
                            ]}
                        ]
                    }
                ) {
                    id
                    predicate {
                        label
                    }
                    object {
                        label
                    }
                }
            }
            '''
            async with session.post(
                GRAPHQL_URL,
                json={'query': query_builder, 'variables': {'address': addr}},
                headers={'Content-Type': 'application/json'}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    builder_triples = data.get('data', {}).get('triples', [])
                    result['verified_builder'] = len(builder_triples) > 0
        except Exception as e:
            logger.warning(f'Failed to fetch builder status for {addr}: {e}')

    # Calculate trust score
    result['trust_score'] = calculate_trust_score(result)

    # Extract categories from attestations
    result['categories'] = extract_categories(result)

    return result


def calculate_trust_score(data: Dict[str, Any]) -> float:
    """Calculate trust score based on attestations with predicate weights and time decay."""
    score = 0.0

    # Score from attestations received
    for att in data.get('attestations_received', []):
        predicate = att.get('predicate', {}).get('label', '')
        weight = get_predicate_weight(predicate)

        # Time decay
        created_at = att.get('created_at', '')
        time_factor = calculate_time_decay(created_at) if created_at else 1.0

        # Stake weight (vault shares indicate conviction)
        vault_shares = float(att.get('vault', {}).get('total_shares', 0) or 0) / 1e18
        stake_bonus = min(vault_shares / 10, 1.0)  # Cap at 1.0 bonus

        attestation_score = weight * time_factor * (1 + stake_bonus)
        score += attestation_score

    # Bonus for vouches (high trust signals)
    vouch_count = len(data.get('vouches', []))
    score += vouch_count * 5.0

    # Bonus for verified builder status
    if data.get('verified_builder'):
        score += 10.0

    # Activity bonus (capped)
    activity = data.get('activity_count', 0)
    score += min(activity / 10, 5.0)

    # Staking bonus
    staked = data.get('staked', 0)
    if staked > 100:
        score += 5.0
    elif staked > 10:
        score += 2.0
    elif staked > 0:
        score += 1.0

    return round(max(score, 0), 2)


def extract_categories(data: Dict[str, Any]) -> List[str]:
    """Extract category labels from attestations."""
    categories = set()

    for att in data.get('attestations_received', []):
        obj_label = att.get('object', {}).get('label', '')
        if obj_label:
            # Common category patterns
            lower = obj_label.lower()
            if any(x in lower for x in ['defi', 'finance', 'lending', 'dex', 'yield']):
                categories.add('DeFi')
            if any(x in lower for x in ['nft', 'art', 'collectible']):
                categories.add('NFT')
            if any(x in lower for x in ['dao', 'governance', 'vote']):
                categories.add('DAO')
            if any(x in lower for x in ['builder', 'developer', 'engineer', 'code']):
                categories.add('Builder')
            if any(x in lower for x in ['verified', 'trusted', 'legitimate']):
                categories.add('Verified')
            if any(x in lower for x in ['community', 'contributor', 'member']):
                categories.add('Community')

    return list(categories)


# =============================================================================
# LLM SUMMARIZATION (Anthropic Claude API)
# =============================================================================

async def generate_llm_summary(data: Dict[str, Any]) -> str:
    """Generate natural language summary using Claude API."""
    if not ANTHROPIC_API_KEY:
        return generate_fallback_summary(data)

    # Prepare context for Claude
    context = prepare_llm_context(data)

    prompt = f"""You are analyzing an Intuition Network user's on-chain reputation data. Generate a concise, natural language summary (2-3 sentences max) about this user's reputation and trust profile.

Data:
{json.dumps(context, indent=2)}

Guidelines:
- Start with the most important trust signal (verified builder, high vouch count, etc.)
- Mention specific categories they're trusted in (DeFi, NFT, DAO, etc.)
- Note who vouched for them if notable (verified builders, known entities)
- Include activity timeline if available ("active since 2024")
- Be factual and specific, not vague
- If trust score is low or negative, mention concerns

Example outputs:
- "Highly trusted DeFi contributor vouched by 5 verified builders. Active since Q1 2024 with strong staking conviction."
- "Community member with moderate trust in NFT space. Limited attestations but consistent activity."
- "New account with no attestations yet. Consider verifying identity before trusting."

Generate the summary now:"""

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                'https://api.anthropic.com/v1/messages',
                headers={
                    'Content-Type': 'application/json',
                    'x-api-key': ANTHROPIC_API_KEY,
                    'anthropic-version': '2023-06-01'
                },
                json={
                    'model': 'claude-sonnet-4-20250514',
                    'max_tokens': 200,
                    'messages': [
                        {'role': 'user', 'content': prompt}
                    ]
                }
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    content = result.get('content', [])
                    if content and len(content) > 0:
                        return content[0].get('text', generate_fallback_summary(data))
                else:
                    error_text = await resp.text()
                    logger.warning(f'Claude API error: {resp.status} - {error_text}')
    except Exception as e:
        logger.warning(f'Failed to generate LLM summary: {e}')

    return generate_fallback_summary(data)


def prepare_llm_context(data: Dict[str, Any]) -> Dict[str, Any]:
    """Prepare structured context for LLM."""
    # Extract vouch details
    vouchers = []
    for v in data.get('vouches', [])[:5]:
        creator = v.get('creator', {})
        vouchers.append({
            'from': creator.get('label') or creator.get('id', '')[:10],
            'predicate': v.get('predicate', {}).get('label', ''),
        })

    # Extract key attestations
    key_attestations = []
    for att in data.get('attestations_received', [])[:10]:
        predicate = att.get('predicate', {}).get('label', '')
        obj = att.get('object', {}).get('label', '')
        if predicate or obj:
            key_attestations.append({
                'predicate': predicate,
                'object': obj,
            })

    # Parse first activity year
    first_activity_year = None
    if data.get('first_activity'):
        try:
            dt = datetime.fromisoformat(data['first_activity'].replace('Z', '+00:00'))
            first_activity_year = dt.year
        except Exception:
            pass

    return {
        'label': data.get('label'),
        'trust_score': data.get('trust_score'),
        'staked_amount': round(data.get('staked', 0), 2),
        'activity_count': data.get('activity_count'),
        'vouch_count': len(data.get('vouches', [])),
        'vouchers': vouchers,
        'attestation_count': len(data.get('attestations_received', [])),
        'key_attestations': key_attestations,
        'categories': data.get('categories', []),
        'verified_builder': data.get('verified_builder'),
        'first_activity_year': first_activity_year,
        'attestations_given_count': len(data.get('attestations_given', [])),
    }


def generate_fallback_summary(data: Dict[str, Any]) -> str:
    """Generate a basic summary when LLM is unavailable."""
    parts = []

    trust_score = data.get('trust_score', 0)
    if trust_score >= 20:
        parts.append("Highly trusted user")
    elif trust_score >= 10:
        parts.append("Moderately trusted user")
    elif trust_score > 0:
        parts.append("New or lightly attested user")
    else:
        parts.append("Unattested account")

    categories = data.get('categories', [])
    if categories:
        parts.append(f"active in {', '.join(categories[:3])}")

    vouch_count = len(data.get('vouches', []))
    if vouch_count > 0:
        parts.append(f"vouched by {vouch_count} account{'s' if vouch_count != 1 else ''}")

    if data.get('verified_builder'):
        parts.append("verified builder")

    staked = data.get('staked', 0)
    if staked > 0:
        parts.append(f"{staked:,.2f} TRUST staked")

    return ". ".join(parts) + "." if parts else "No reputation data available."


# =============================================================================
# DISCORD SLASH COMMANDS
# =============================================================================

@bot.event
async def on_ready():
    """Called when bot is ready and connected to Discord."""
    await db.init()

    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        logger.info(f'Synced {len(synced)} slash command(s)')
    except Exception as e:
        logger.error(f'Failed to sync commands: {e}')

    logger.info(f'Bot connected as {bot.user} (ID: {bot.user.id})')
    logger.info(f'Connected to {len(bot.guilds)} guild(s)')
    logger.info(f'Database: {"PostgreSQL" if db.use_postgres else "SQLite"}')
    logger.info(f'LLM: {"Enabled" if ANTHROPIC_API_KEY else "Disabled (fallback mode)"}')


# /link command - Link Discord user to wallet
@bot.tree.command(name='link', description='Link your Discord account to an Ethereum wallet')
@app_commands.describe(wallet_address='Your Ethereum wallet address (0x...)')
async def link_command(interaction: discord.Interaction, wallet_address: str):
    """Link Discord user to wallet address."""
    await interaction.response.defer(ephemeral=True)

    wallet = wallet_address.strip().lower()

    if not is_valid_address(wallet):
        await interaction.followup.send(
            'Invalid wallet address. Must be a valid Ethereum address (0x... 42 characters).',
            ephemeral=True
        )
        return

    success = await db.link_wallet(interaction.user.id, wallet)

    if success:
        short_addr = f'{wallet[:6]}...{wallet[-4:]}'
        logger.info(f'User {interaction.user.id} linked to {short_addr}')
        await interaction.followup.send(
            f'Successfully linked your Discord account to `{short_addr}`',
            ephemeral=True
        )
    else:
        await interaction.followup.send(
            'Failed to link wallet. Please try again.',
            ephemeral=True
        )


# /unlink command - Remove wallet link
@bot.tree.command(name='unlink', description='Unlink your wallet from your Discord account')
async def unlink_command(interaction: discord.Interaction):
    """Remove wallet link for Discord user."""
    await interaction.response.defer(ephemeral=True)

    success = await db.unlink_wallet(interaction.user.id)

    if success:
        logger.info(f'User {interaction.user.id} unlinked wallet')
        await interaction.followup.send(
            'Successfully unlinked your wallet.',
            ephemeral=True
        )
    else:
        await interaction.followup.send(
            'No wallet linked to your account.',
            ephemeral=True
        )


# /rep command - Reputation lookup with LLM summary
@bot.tree.command(name='rep', description='Look up Intuition reputation for a user or wallet')
@app_commands.describe(
    user='Discord user to look up (must have linked wallet)',
    wallet='Ethereum wallet address to look up directly'
)
async def rep_command(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None,
    wallet: Optional[str] = None
):
    """Fetch Intuition reputation with LLM summary."""
    await interaction.response.defer()

    target_wallet = None
    display_name = None

    # Determine target wallet
    if wallet:
        wallet = wallet.strip().lower()
        if not is_valid_address(wallet):
            await interaction.followup.send('Invalid wallet address format.')
            return
        target_wallet = wallet
        display_name = f'{wallet[:6]}...{wallet[-4:]}'
    elif user:
        target_wallet = await db.get_wallet_by_discord_id(user.id)
        if not target_wallet:
            await interaction.followup.send(
                f'{user.display_name} has not linked a wallet. They can use `/link` to connect one.'
            )
            return
        display_name = user.display_name
    else:
        # Look up the command user's own wallet
        target_wallet = await db.get_wallet_by_discord_id(interaction.user.id)
        if not target_wallet:
            await interaction.followup.send(
                'You have not linked a wallet. Use `/link <wallet_address>` first, or specify a wallet directly.'
            )
            return
        display_name = interaction.user.display_name

    # Fetch comprehensive data
    try:
        data = await fetch_comprehensive_data(target_wallet)
    except Exception as e:
        logger.error(f'Failed to fetch data for {target_wallet}: {e}')
        await interaction.followup.send('Failed to fetch Intuition data. Please try again.')
        return

    # Generate LLM summary
    summary = await generate_llm_summary(data)

    # Build embed
    embed = discord.Embed(
        title=f'Intuition Reputation: {display_name}',
        description=summary,
        color=get_trust_color(data['trust_score'])
    )

    # Identity label if available
    if data.get('label'):
        embed.set_author(name=data['label'], icon_url='https://intuition.systems/favicon.ico')

    # Trust score with visual indicator
    trust_emoji = get_trust_emoji(data['trust_score'])
    embed.add_field(
        name='Trust Score',
        value=f'{trust_emoji} **{data["trust_score"]}**',
        inline=True
    )

    # Vouches
    vouch_count = len(data.get('vouches', []))
    embed.add_field(
        name='Vouches',
        value=str(vouch_count),
        inline=True
    )

    # Attestations
    att_count = len(data.get('attestations_received', []))
    embed.add_field(
        name='Attestations',
        value=str(att_count),
        inline=True
    )

    # TRUST staked
    embed.add_field(
        name='TRUST Staked',
        value=f'{data["staked"]:,.2f}',
        inline=True
    )

    # Activity
    embed.add_field(
        name='Activity',
        value=f'{data["activity_count"]} actions',
        inline=True
    )

    # Categories
    categories = data.get('categories', [])
    if categories:
        embed.add_field(
            name='Categories',
            value=', '.join(categories),
            inline=True
        )

    # Builder badge
    if data.get('verified_builder'):
        embed.add_field(
            name='Status',
            value='Verified Builder',
            inline=True
        )

    # Wallet address (shortened)
    short_wallet = f'{target_wallet[:6]}...{target_wallet[-4:]}'
    embed.set_footer(text=f'Wallet: {short_wallet} | Intuition Mainnet')

    await interaction.followup.send(embed=embed)
    logger.info(f'Rep lookup for {display_name} ({short_wallet}): score={data["trust_score"]}')


def get_trust_color(score: float) -> int:
    """Get embed color based on trust score."""
    if score >= 20:
        return 0x00FF00  # Green - High trust
    elif score >= 10:
        return 0x5865F2  # Discord blue - Good trust
    elif score >= 5:
        return 0xFFAA00  # Orange - Moderate
    elif score > 0:
        return 0xFFFF00  # Yellow - Low
    else:
        return 0xFF0000  # Red - No/negative trust


def get_trust_emoji(score: float) -> str:
    """Get emoji indicator for trust score."""
    if score >= 20:
        return '🟢'
    elif score >= 10:
        return '🔵'
    elif score >= 5:
        return '🟡'
    elif score > 0:
        return '🟠'
    else:
        return '🔴'


# /stats command - Bot statistics
@bot.tree.command(name='stats', description='Show bot statistics')
async def stats_command(interaction: discord.Interaction):
    """Show bot statistics."""
    embed = discord.Embed(
        title='Intuition Rep Bot',
        color=0x5865F2
    )
    embed.add_field(name='Servers', value=str(len(bot.guilds)), inline=True)
    embed.add_field(name='Database', value='PostgreSQL' if db.use_postgres else 'SQLite', inline=True)
    embed.add_field(name='LLM', value='Enabled' if ANTHROPIC_API_KEY else 'Fallback', inline=True)
    embed.add_field(name='GraphQL', value=GRAPHQL_URL.split('//')[-1].split('/')[0], inline=False)
    embed.set_footer(text='Intuition Network | Trust Scoring v2')
    await interaction.response.send_message(embed=embed)


# Legacy prefix commands for backwards compatibility
@bot.command(name='rep')
async def legacy_rep(ctx, identifier: str = None):
    """Legacy prefix command - directs to slash command."""
    await ctx.send('This bot now uses slash commands! Use `/rep` instead.')


@bot.command(name='link')
async def legacy_link(ctx, *args):
    """Legacy prefix command - directs to slash command."""
    await ctx.send('This bot now uses slash commands! Use `/link <wallet_address>` instead.')


# =============================================================================
# MAIN
# =============================================================================

def main():
    """Main entry point."""
    if not TOKEN:
        logger.error('DISCORD_TOKEN not set in environment variables')
        return

    logger.info('Starting Intuition Rep Bot v2...')
    logger.info(f'GraphQL Endpoint: {GRAPHQL_URL}')
    logger.info(f'Database Mode: {"PostgreSQL" if DATABASE_URL else "SQLite"}')
    logger.info(f'LLM Mode: {"Anthropic Claude" if ANTHROPIC_API_KEY else "Fallback (no API key)"}')

    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        logger.error('Invalid Discord token')
    except Exception as e:
        logger.error(f'Bot error: {type(e).__name__}: {e}')


if __name__ == '__main__':
    main()
