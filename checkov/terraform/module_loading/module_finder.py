import logging
import os
import re
from concurrent import futures
from typing import List, Callable, Dict

from checkov.terraform.module_loading.registry import module_loader_registry


class ModuleDownload:
    source_dir: str
    module_link: str = None
    version: str = None

    def __init__(self, source_dir):
        self.source_dir = source_dir

    def __str__(self):
        return f"{self.source_dir} -> {self.module_link} ({self.version})"

    @property
    def address(self):
        return f'{self.module_link}:{self.version}'


def find_modules(path: str) -> List[ModuleDownload]:
    modules_found = []
    for root, dir_names, full_file_names in os.walk(path):
        for file_name in full_file_names:
            if not file_name.endswith('.tf'):
                continue
            with open(os.path.join(path, root, file_name)) as f:
                try:
                    in_module = False
                    curr_md = None
                    for line in f:
                        if not in_module:
                            if line.startswith('module'):
                                in_module = True
                                curr_md = ModuleDownload(os.path.dirname(os.path.join(root, file_name)))
                                continue
                        if in_module:
                            if line.startswith('}'):
                                in_module = False
                                if curr_md.module_link is None:
                                    logging.warning(f'A module at {curr_md.source_dir} had no source, skipping')
                                else:
                                    modules_found.append(curr_md)
                                curr_md = None
                                continue

                            match = re.match('.*\\bsource\\s*=\\s*"(?P<LINK>.*)"', line)
                            if match:
                                curr_md.module_link = match.group('LINK')
                                continue

                            match = re.match('.*\\bversion\\s*=\\s*"[^\\d]*(?P<VERSION>.*)"', line)
                            if match:
                                curr_md.version = match.group('VERSION')
                except (UnicodeDecodeError, FileNotFoundError) as e:
                    logging.warning(f"Skipping {os.path.join(path, root, file_name)} because of {str(e)}")
                    continue

    return modules_found


def should_download(path: str) -> bool:
    return not (path.startswith('./') or path.startswith('../') or path.startswith('/'))


def load_tf_modules(path: str, should_download_module: Callable[[str], bool] = should_download):
    module_loader_registry.download_external_modules = True
    module_loader_registry.root_dir = path
    modules_to_load = find_modules(path)

    def _download_module(m):
        if should_download_module(m.module_link):
            logging.info(f'Downloading module {m.address}')
            try:
                content = module_loader_registry.load(m.source_dir, m.module_link,
                                                      "latest" if not m.version else m.version)
                if content is None or not content.loaded():
                    logging.warning(f'Failed to download module {m.address}')
            except Exception as e:
                logging.warning("Unable to load module (%s): %s", m.address, e)

    # To avoid duplicate work, we need to get the distinct module sources
    distinct_modules = {m.address: m for m in modules_to_load}.values()

    # To get the modules without conllisions and constraints, we need to make sure we don't run git commands on the
    # same repository. It seems to break things as git might not be thread safe.
    batches: List[Dict[str, ModuleDownload]] = []
    for m in distinct_modules:
        found = -1
        for i, batch in enumerate(batches):
            if m.module_link in batch:
                found = i
            else:
                break
        if found == len(batches) - 1:
            batches.append({m.module_link: m})
        else:
            batches[found + 1][m.module_link] = m

    for b in batches:
        with futures.ThreadPoolExecutor() as executor:
            futures.wait(
                [executor.submit(_download_module, m) for m in b.values()],
                return_when=futures.ALL_COMPLETED,
            )
