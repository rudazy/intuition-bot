# Intuition Rep Bot

A Discord bot for querying Intuition Protocol reputation data.

## Features

- Link wallet addresses to nicknames
- Query TRUST staked, network activity, and utilization
- Direct wallet address lookup support

## Commands

| Command | Description |
|---------|-------------|
| `!link <wallet> <nickname>` | Link a wallet to a nickname |
| `!rep <nickname>` | Get Intuition stats for a nickname |
| `!rep <wallet>` | Get Intuition stats for a wallet directly |
| `!unlink <nickname>` | Remove a nickname from registry |

## Setup

1. Clone the repository
2. Copy `.env.example` to `.env`
3. Add your Discord bot token
4. Install dependencies: `pip install -r requirements.txt`
5. Run: `python bot.py`

## Environment Variables

| Variable | Description |
|----------|-------------|
| `DISCORD_TOKEN` | Discord bot token from Developer Portal |
| `INTUITION_GRAPHQL_URL` | GraphQL endpoint (default: mainnet) |
| `INTUITION_RPC_URL` | RPC endpoint for Intuition Network |
| `DATABASE_URL` | PostgreSQL URL (optional, uses SQLite locally) |

## Deployment (Railway)

1. Push to GitHub
2. Connect repository to Railway
3. Add environment variables in Railway dashboard
4. Deploy

## Network Info

- Mainnet GraphQL: `https://mainnet.intuition.sh/v1/graphql`
- Mainnet RPC: `https://rpc.intuition.systems`
- Chain ID: 1155

## License

MIT