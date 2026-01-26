# Intuition Rep Bot v2

Discord bot for querying Intuition Network reputation data with advanced trust scoring and AI-powered summaries.

## Features

- **Wallet-Discord Linking**: Link your Discord account to an Ethereum wallet
- **Trust Scoring**: Advanced scoring with predicate weights, time decay, and stake conviction
- **LLM Summaries**: Natural language reputation summaries powered by Claude
- **Slash Commands**: Modern Discord slash command interface

## Commands

| Command | Description |
|---------|-------------|
| `/link <wallet_address>` | Link your Discord account to a wallet |
| `/unlink` | Remove your wallet link |
| `/rep` | Look up your own Intuition reputation |
| `/rep @user` | Look up another user's reputation (requires linked wallet) |
| `/rep wallet:<address>` | Direct wallet lookup |
| `/stats` | Show bot statistics |

## Trust Scoring

The bot calculates trust scores using:

### Predicate Weights
- **High (1.5x)**: trusted, verified, endorsed, vouched, recommended, collaborated, core contributor, builder
- **Medium (1.0x)**: known, followed, member, worked with, affiliated, connected, associated
- **Low (0.5x)**: aware of, met, interested in, interacted with
- **Negative (-2.0x)**: distrusted, flagged, reported, blocked, scam, suspicious, fake

### Additional Factors
- **Time Decay**: Attestations older than 1 year receive 50% weight
- **Stake Conviction**: Vault shares boost attestation weight
- **Vouch Bonus**: +5 points per vouch
- **Builder Status**: +10 points for verified builders
- **Activity Bonus**: Up to +5 points based on network activity

## Setup

1. Clone the repository
2. Copy `.env.example` to `.env` and fill in:
   - `DISCORD_TOKEN`: Your Discord bot token
   - `ANTHROPIC_API_KEY`: Your Anthropic API key (optional, enables LLM summaries)
   - `DATABASE_URL`: PostgreSQL connection string (optional, defaults to SQLite)

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Run the bot:
   ```bash
   python bot.py
   ```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_TOKEN` | Yes | Discord bot token |
| `ANTHROPIC_API_KEY` | No | Enables Claude LLM summaries |
| `INTUITION_GRAPHQL_URL` | No | Defaults to mainnet |
| `DATABASE_URL` | No | PostgreSQL URL (defaults to SQLite) |

## Database Schema

```sql
CREATE TABLE user_wallets (
    discord_id BIGINT PRIMARY KEY,
    wallet TEXT NOT NULL,
    linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## Network Info

- Mainnet GraphQL: `https://mainnet.intuition.sh/v1/graphql`
- Mainnet RPC: `https://rpc.intuition.systems`
- Chain ID: 1155

## Deployment

### Railway
The bot includes `Procfile` and `railway.toml` for Railway deployment.

### Docker
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
```

## License

MIT
