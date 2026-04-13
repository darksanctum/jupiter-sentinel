#!/bin/bash
# Push Jupiter Sentinel to GitHub
# Run: gh auth login first, then ./push.sh

set -e

echo "Creating GitHub repo..."
gh repo create jupiter-sentinel --public --source=. --push --description "Autonomous AI DeFi agent combining multiple Jupiter APIs - volatility scanner, trade executor, risk manager, arbitrage detector"

echo ""
echo "Repo created! URL: https://github.com/$(gh api user -q .login)/jupiter-sentinel"
echo ""
echo "Now update the Superteam submission with the correct GitHub link."
