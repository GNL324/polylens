import argparse

def analyze_wallet(wallet: str):
    print("Polylens Wallet Analyzer")
    print("=" * 28)
    print(f"Wallet: {wallet}")
    print()
    print("Next step: connect Polymarket wallet data adapter.")

def main():
    parser = argparse.ArgumentParser(prog="polylens")
    sub = parser.add_subparsers(dest="command", required=True)

    wallet_parser = sub.add_parser("analyze-wallet")
    wallet_parser.add_argument("wallet")

    args = parser.parse_args()

    if args.command == "analyze-wallet":
        analyze_wallet(args.wallet)

if __name__ == "__main__":
    main()
