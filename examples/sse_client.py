import argparse
import json
from urllib.request import urlopen


def iter_sse(response):
    event_name = 'message'
    event_id = None
    data_lines = []

    for raw_line in response:
        line = raw_line.decode('utf-8').rstrip('\r\n')

        if line == '':
            if data_lines:
                yield {
                    'event': event_name,
                    'id': event_id,
                    'data': '\n'.join(data_lines),
                }
            event_name = 'message'
            event_id = None
            data_lines = []
            continue

        if line.startswith(':'):
            continue

        field, _, value = line.partition(':')
        if value.startswith(' '):
            value = value[1:]

        if field == 'event':
            event_name = value
        elif field == 'id':
            event_id = value
        elif field == 'data':
            data_lines.append(value)


def main():
    parser = argparse.ArgumentParser(description='Minimal SSE client for stream-translator-gpt.')
    parser.add_argument('url', nargs='?', default='http://127.0.0.1:18000/events', help='SSE endpoint URL.')
    args = parser.parse_args()

    with urlopen(args.url) as response:
        for event in iter_sse(response):
            payload = event['data']
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                parsed = payload

            print(f'event={event["event"]} id={event["id"]}')
            if isinstance(parsed, dict):
                print(json.dumps(parsed, ensure_ascii=False, indent=2))
            else:
                print(parsed)
            print()


if __name__ == '__main__':
    main()
