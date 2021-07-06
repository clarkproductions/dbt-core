from dbt.context.context_config import ContextConfig
from dbt.contracts.graph.parsed import ParsedModelNode
import dbt.flags as flags
from dbt.node_types import NodeType
from dbt.parser.base import SimpleSQLParser
from dbt.parser.search import FileBlock
from dbt_extractor import ExtractionError, py_extract_from_source  # type: ignore
import itertools
import random
from typing import Any, Dict, List, Set, Tuple


class ModelParser(SimpleSQLParser[ParsedModelNode]):
    def parse_from_dict(self, dct, validate=True) -> ParsedModelNode:
        if validate:
            ParsedModelNode.validate(dct)
        return ParsedModelNode.from_dict(dct)

    @property
    def resource_type(self) -> NodeType:
        return NodeType.Model

    @classmethod
    def get_compiled_path(cls, block: FileBlock):
        return block.path.relative_path

    def render_update(
        self, node: ParsedModelNode, config: ContextConfig
    ) -> None:
        self.manifest._parsing_info.static_analysis_path_count += 1

        # `True` roughly 1/100 times this function is called
        sample: bool = random.randint(1, 101) == 100

        # run the experimental parser if the flag is on or if we're sampling
        if flags.USE_EXPERIMENTAL_PARSER or sample:
            try:
                experimentally_parsed: Dict[str, List[Any]] = py_extract_from_source(node.raw_sql)

                # second config format
                config_calls: List[Dict[str, str]] = []
                for c in experimentally_parsed['configs']:
                    config_calls.append({c[0]: c[1]})

                # format sources TODO change extractor to match this type
                source_calls: List[List[str]] = []
                for s in experimentally_parsed['sources']:
                    source_calls.append([s[0], s[1]])
                experimentally_parsed['sources'] = source_calls

            except ExtractionError as e:
                experimentally_parsed = e

        # normal dbt run
        if not flags.USE_EXPERIMENTAL_PARSER:
            # normal rendering
            super().render_update(node, config)
            # if we're sampling, compare for correctness
            if sample:
                result: Set[str] = set()
                # experimental parser couldn't parse
                if isinstance(experimentally_parsed, Exception):
                    result.add("01_experimental_parser_cannot_parse")
                else:
                    # rearrange existing configs to match:
                    real_configs: List[Tuple[str, Any]] = list(
                        itertools.chain.from_iterable(
                            map(lambda x: x.items(), config._config_calls)
                        )
                    )

                    # look for false positive configs
                    for c in experimentally_parsed['configs']:
                        if c not in real_configs:
                            result.add("02_false_positive_config_value")
                            break

                    # look for missed configs
                    for c in real_configs:
                        if c not in experimentally_parsed['configs']:
                            result.add("03_missed_config_value")
                            break

                    # look for false positive sources
                    for s in experimentally_parsed['sources']:
                        if s not in node.sources:
                            result.add("04_false_positive_source_value")
                            break

                    # look for missed sources
                    for s in node.sources:
                        if s not in experimentally_parsed['sources']:
                            result.add("05_missed_source_value")
                            break

                    # look for false positive refs
                    for r in experimentally_parsed['refs']:
                        if r not in node.refs:
                            result.add("06_false_positive_ref_value")
                            break

                    # look for missed refs
                    for r in node.refs:
                        if r not in experimentally_parsed['refs']:
                            result.add("07_missed_ref_value")
                            break

                    # if there are no errors, return a success value
                    if not result:
                        result = set(["00_exact_match"])

                    # set sample results. results are cumulative across the dbt run,
                    # so we will only know there exists a false positive config value
                    # somewhere in the project, not how many files have false positve
                    # configs. this means a perfectly correct sample will return a
                    # result of {'00_exact_match'}.
                    self.manifest._parsing_info.static_analysis_result.update(result)

        # if the --use-experimental-parser flag was set, and the experimental parser succeeded
        elif not isinstance(experimentally_parsed, Exception):
            # since it doesn't need python jinja, fit the refs, sources, and configs
            # into the node. Down the line the rest of the node will be updated with
            # this information. (e.g. depends_on etc.)
            config._config_calls = config_calls

            # this uses the updated config to set all the right things in the node.
            # if there are hooks present, it WILL render jinja. Will need to change
            # when the experimental parser supports hooks
            self.update_parsed_node(node, config)

            # update the unrendered config with values from the file.
            # values from yaml files are in there already
            node.unrendered_config.update(dict(experimentally_parsed['configs']))

            # set refs, sources, and configs on the node object
            node.refs += experimentally_parsed['refs']
            node.sources += experimentally_parsed['sources']
            for configv in experimentally_parsed['configs']:
                node.config[configv[0]] = configv[1]

            self.manifest._parsing_info.static_analysis_parsed_path_count += 1

        # the experimental parser tried and failed on this model.
        # fall back to python jinja rendering.
        else:
            super().render_update(node, config)
