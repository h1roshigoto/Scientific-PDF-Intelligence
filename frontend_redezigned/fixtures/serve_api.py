"""Run the unchanged FastAPI app with isolated, explicitly synthetic QA data."""
import argparse
import json
import os
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory

import uvicorn

from spi.pdf import import_pdf
from spi.storage import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8001)
    args = parser.parse_args()
    fixtures = Path(__file__).resolve().parent
    demo = json.loads((fixtures / 'demo.json').read_text(encoding='utf-8'))
    print('Synthetic QA data only. No production state is read or written.', flush=True)
    with TemporaryDirectory(prefix='spi-frontend-qa-') as isolated:
        root = Path(isolated)
        store = Store(root / 'state')
        for doc in demo['documents']:
            track, pid = doc['document_id'].split(':', maxsplit=1)
            directory = root / track
            directory.mkdir(exist_ok=True)
            pdf = directory / f'{pid}.pdf'
            copyfile(fixtures.parent / 'public' / 'demo' / f'{track}-{pid}.pdf', pdf)
            record = import_pdf(store, pdf, track)
            store.upsert('documents.json', {**record, **doc}, 'document_id')
        for artifact in ('results', 'issues'):
            store.write(f'{artifact}.json', demo[artifact])
        os.environ['SPI_STATE_DIR'] = str(store.root)
        uvicorn.run('spi.api:app', host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
