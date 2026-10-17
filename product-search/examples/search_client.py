"""Call the search server from a separate Chat/Profile client process."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from search_client import SearchClient


async def run(args: argparse.Namespace) -> None:
    async with SearchClient(args.base_url, source=args.source) as client:
        health = await client.ready()
        result = await client.search({
            'query': args.query,
            'filters': {'maxPrice': args.max_price},
            'limit': 5,
        }, snapshot_id=health['snapshotId'])
        ids = [hit['productId'] for hit in result['hits']]
        details = await client.get_products(ids, snapshot_id=result['snapshotId']) if ids else {
            'products': [], 'missingIds': [],
        }
        print(json.dumps({
            'source': args.source,
            'ready': health['status'],
            'snapshotId': result['snapshotId'],
            'status': result['status'],
            'timing': result['timing'],
            'products': [{
                'productId': hit['productId'],
                'sourceProductId': hit['sourceProductId'],
                'name': hit['name'],
                'price': hit['price'],
                'availability': hit['availability'],
            } for hit in result['hits']],
            'details': len(details['products']),
            'missingIds': details['missingIds'],
        }, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:4325')
    parser.add_argument('--source', choices=['chat', 'profile'], default='chat')
    parser.add_argument('--query', default='텀블러')
    parser.add_argument('--max-price', type=int, default=50000)
    asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    main()
