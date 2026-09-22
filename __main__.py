"""Command-line entry point for the Invoice Vault application.

This module intentionally keeps the command-line layer small: it parses options,
creates the storage object, and either creates a business or starts the server.
The business key is printed only during creation, so the operator can save it
privately without storing the plaintext key in the database.
"""
import argparse
from pathlib import Path

from .server import Server
from .store import Store


DEFAULT_PORT = 7755


def main():
    """Parse a command and run the requested administrator operation."""
    parser = argparse.ArgumentParser(description='Invoice Vault local prototype')
    parser.add_argument('--data-dir', type=Path, default=Path.home() / '.invoice-vault',
                        help='Private persistent storage, outside the source repository by default')
    commands = parser.add_subparsers(dest='command', required=True)

    # Business creation is deliberately separate from serving so an administrator
    # can create an account once, copy the one-time key to a password manager,
    # and then start the local web server.
    create = commands.add_parser('create-business', help='Create a business and print its access key once')
    create.add_argument('name')

    serve = commands.add_parser('serve', help='Start the loopback-only web app')
    serve.add_argument('--port', type=int, default=DEFAULT_PORT)
    serve.add_argument('--public-origin', help='Exact HTTPS origin when behind a configured reverse proxy')
    args = parser.parse_args()

    try:
        store = Store(args.data_dir)
        if args.command == 'create-business':
            business = store.create_business(args.name)
            print('Business:', business['name'])
            print('Access key (save privately; grants full access to this business):', business['token'])
            return

        if not 1 <= args.port <= 65535:
            parser.error('Port must be between 1 and 65535.')

        # Server enforces loopback binding. A reverse proxy, when configured,
        # may use public_origin for strict Host and Origin validation.
        server = Server(('127.0.0.1', args.port), store, args.public_origin)
        print('Invoice Vault:', server.origin, flush=True)
        print('Local prototype. Do not expose the Python server directly to the internet.', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    except (ValueError, OSError) as error:
        # Turn expected setup errors into a concise CLI message rather than a traceback.
        parser.error(str(error))


if __name__ == '__main__':
    main()
