import os
from collections import defaultdict
from importlib import resources
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

import typer
from grep_ast import TreeContext, filename_to_lang
from grep_ast.tsl import get_language, get_parser
from tqdm import tqdm
from tree_sitter import Query, QueryCursor

from opsmith.constants import ROOT_IMPORTANT_FILES
from opsmith.git_repo import GitRepo
from opsmith.utils import WaitingSpinner


class Tag(NamedTuple):
    rel_filename: str
    filename: str
    line: int
    name: str
    kind: str


REPO_MAP_MESSAGE = "Generating repo map"


def get_scm_filename(lang: str) -> Optional[Path]:
    """
    Retrieve the filename of the `.scm` (S-expression-based queries) file for the
    specified programming language from the package's resource directory.

    This function attempts to locate a file corresponding to the given language's
    tags, stored in the "tree-sitter-languages" subdirectory under the "queries"
    directory within the package. If the file exists, its corresponding `Path`
    object is returned. If the file cannot be found or an error occurs while
    accessing resources, the function returns `None`.

    :param lang: The name of the programming language for which the `.scm` file
        is being queried.
    :type lang: str
    :return: A `Path` object pointing to the `.scm` file if it exists, or `None`
        if the file is not found or an error occurs.
    :rtype: Optional[Path]
    """

    # Try tree-sitter-language-pack subdir first
    try:
        path = resources.files(__package__).joinpath(
            "queries", "tree-sitter-languages", f"{lang}-tags.scm"
        )
        if path.is_file():
            return path
    except KeyError:
        pass  # Silently continue if path doesn't exist or package structure is not as expected

    return None


class RepoMap:
    warned_files: Set[str]

    def __init__(
        self,
        src_dir: str,
        map_tokens: int = 5120,
        max_tags_depth: int = 2,
        repo_content_prefix: Optional[
            str
        ] = "This repo map contains a list of files and important symbols.\n\n",
        verbose: bool = False,
    ):
        """
        Initializes an instance of the class, which sets up configuration and parameters
        required for processing a repository. It validates paths, initializes relevant
        attributes for managing warnings and tag depths, and optionally logs verbose
        messages if enabled.

        :param src_dir: The directory path of the repository to be processed.
        :param map_tokens: The maximum number of tokens allowed in the mapping
            process.
        :param max_tags_depth: The maximum depth level for tagging content within
            the repository.
        :param repo_content_prefix: An optional prefix string that specifies initial
            details or descriptions before mapping repository content.
        :param verbose: A flag indicating if detailed logging should be enabled.
        """
        self.src_dir = Path(src_dir).resolve()
        self.git_repo = GitRepo(self.src_dir)
        self.verbose = verbose
        self.tracked_files: List[Path] = self.git_repo.get_git_tracked_files(
            [str(self.src_dir), ":!**/*test*"]
        )

        # Initialize tracking variables
        self.warned_files = set()

        self.max_map_tokens = map_tokens
        self.max_tags_depth = max_tags_depth
        self.repo_content_prefix = repo_content_prefix if repo_content_prefix is not None else ""

        self._warned_missing_scm = set()
        if self.verbose:
            typer.echo(f"RepoMap initialized for {self.src_dir}")

    @staticmethod
    def _simple_token_count(text: str) -> int:
        """A very rough estimate of token count."""
        return len(text) // 4  # Common heuristic: 1 token ~ 4 chars

    def _token_count(self, text: str) -> int:
        """
        Estimates token count. For large texts, it samples to speed up.
        Uses a simple character-based heuristic.
        """
        len_text = len(text)
        if len_text == 0:
            return 0
        if len_text < 200:  # For small texts, count directly
            return self._simple_token_count(text)

        # For larger texts, sample to estimate
        lines = text.splitlines(keepends=True)
        num_lines = len(lines)
        if num_lines == 0:
            return 0

        step = num_lines // 100 or 1  # Sample ~100 lines
        sampled_lines = lines[::step]
        sample_text = "".join(sampled_lines)

        if not sample_text:  # handle case where sampling results in empty text
            return self._simple_token_count(text)  # fallback to full count

        sample_tokens = self._simple_token_count(sample_text)

        # Extrapolate from sample to full text
        # Ensure len(sample_text) is not zero to avoid division by zero
        if len(sample_text) > 0:
            est_tokens = (sample_tokens / len(sample_text)) * len_text
        else:  # if sample_text is empty (e.g. all sampled lines were empty)
            est_tokens = self._simple_token_count(text)  # fallback to full text estimate

        return int(est_tokens)

    def get_repo_map(
        self,
    ) -> Optional[str]:
        if self.max_map_tokens <= 0:
            return None  # Repo map is disabled

        all_tracked_files_paths = self.tracked_files
        if not all_tracked_files_paths:
            if self.verbose:
                typer.echo("RepoMap: No git-tracked files found.", err=True)
            return None  # No files to map

        all_tracked_files_abs_str = [str(p.resolve()) for p in all_tracked_files_paths]

        current_max_map_tokens = self.max_map_tokens

        try:
            with WaitingSpinner(text=REPO_MAP_MESSAGE):
                # Progress within get_tags_map will use spinner.step or tqdm
                files_listing = self.get_tags_map(
                    all_filenames_abs=all_tracked_files_abs_str,
                    max_tokens=current_max_map_tokens,
                )
        except RecursionError:
            typer.echo(
                (
                    "Error: Disabling repo map, recursion depth exceeded. "
                    "Git repo might be too large or complex."
                ),
                err=True,
            )
            self.max_map_tokens = 0  # Disable for future calls
            return None
        except Exception as e:
            typer.echo(f"Error generating repo map: {e}", err=True)
            if self.verbose:
                import traceback

                traceback.print_exc()
            return None

        if not files_listing:
            return None

        if self.verbose:
            num_tokens = self._token_count(files_listing)
            typer.echo(
                f"RepoMap: Final map size {num_tokens / 1024:.1f} k-tokens (estimated)",
                err=True,
            )

        repo_content = self.repo_content_prefix + files_listing
        return repo_content

    def _get_rel_filename(self, filename: str) -> str:
        try:
            return os.path.relpath(filename, str(self.src_dir))
        except ValueError:  # Handles cross-drive issues on Windows
            return filename  # Return absolute path if relpath fails

    @staticmethod
    def _filter_important_files(filenames: List[str]) -> List[str]:
        """
        Filters out the important files from a given list of filenames by checking their names
        against a predefined set of important files.

        Important files are determined by their presence in the `ROOT_IMPORTANT_FILES`.

        :param filenames: List of file paths as strings.
        :type filenames: List[str]

        :return: A filtered list containing only the important file paths that match
            the names in `ROOT_IMPORTANT_FILES`.
        :rtype: List[str]
        """
        priority_files = []
        for filename_str in filenames:
            p_filename = Path(filename_str)
            if p_filename.name in ROOT_IMPORTANT_FILES:
                priority_files.append(filename_str)
        return priority_files

    def _get_tags(self, filename_abs_str: str, rel_filename_str: str) -> List[Tag]:
        # Directly get raw tags without caching
        return list(self._get_tags_raw(filename_abs_str, rel_filename_str))

    def _get_tags_raw(self, filename_abs_str: str, rel_filename_str: str) -> List[Tag]:
        tags = []
        lang = filename_to_lang(filename_abs_str)
        if not lang:
            return tags

        try:
            # These are needed by language.query. grep-ast might handle their loading.
            language_module = get_language(lang)  # from grep_ast.tsl
            parser_module = get_parser(lang)  # from grep_ast.tsl
        except Exception as err:
            # This can happen if tree-sitter binaries/parsers for the lang are not found
            if self.verbose:
                typer.echo(
                    (
                        f"RepoMap: Skipping file {filename_abs_str} for tags (parser/lang init"
                        f" error): {err}"
                    ),
                    err=True,
                )
            return tags

        query_scm_path = get_scm_filename(lang)
        if not query_scm_path or not query_scm_path.exists():
            if self.verbose and lang not in getattr(self, "_warned_missing_scm", set()):
                self._warned_missing_scm.add(lang)
            return tags
        query_scm_content = query_scm_path.read_text(encoding="utf-8")

        try:
            code = Path(filename_abs_str).read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            if self.verbose:
                typer.echo(
                    f"RepoMap: Could not read file {filename_abs_str} for tagging: {e}",
                    err=True,
                )
            return tags

        if not code:
            return tags

        tree = parser_module.parse(bytes(code, "utf-8"))
        query = Query(language_module, query_scm_content)
        captures = QueryCursor(query).captures(tree.root_node)

        processed_tags = set()  # To avoid duplicate tags from different patterns

        saw_kinds = set()
        all_nodes = []
        for tag, nodes in captures.items():
            all_nodes += [(node, tag) for node in nodes]

        for node, tag_name_str in all_nodes:
            # Example tag_name_str: "name.definition.function", "name.reference.class"
            if tag_name_str.startswith("name.definition."):
                kind = "def"
            elif tag_name_str.startswith("name.reference."):
                kind = "ref"
            else:  # Other patterns not directly used for defs/refs
                continue

            saw_kinds.add(kind)
            node_text = node.text.decode("utf-8", "ignore")
            line_no = node.start_point[0]

            tag_tuple = (rel_filename_str, filename_abs_str, line_no, node_text, kind)
            if tag_tuple not in processed_tags:
                tags.append(Tag(*tag_tuple))
                processed_tags.add(tag_tuple)

        return tags

    def _get_all_tags(
        self,
        all_filenames_abs: List[str],  # All git-tracked files, absolute paths
        progress_callback: Optional[callable] = None,
    ) -> List[Tag | Tuple[str]]:
        """ """
        all_tags: List[Tag | Tuple[str]] = []
        if not all_filenames_abs:
            return []

        # Use tqdm for progress if no callback or if verbose
        filenames_iterable = all_filenames_abs
        if (
            self.verbose and not progress_callback
        ):  # Only use tqdm if no specific callback and verbose
            filenames_iterable = tqdm(
                all_filenames_abs,
                desc="Scanning repo files for tags",
                unit="file",
                disable=not self.verbose,
            )

        for i, filename_abs in enumerate(filenames_iterable):
            if progress_callback:
                progress_callback(f"{REPO_MAP_MESSAGE}: Scanning file {Path(filename_abs).name}")

            try:
                if not Path(filename_abs).is_file():
                    if filename_abs not in self.warned_files:
                        typer.echo(
                            f"RepoMap: File not found or not a file: {filename_abs}",
                            err=True,
                        )
                        self.warned_files.add(filename_abs)
                    continue
            except OSError as e:  # Permissions, etc.
                if filename_abs not in self.warned_files:
                    typer.echo(f"RepoMap: OSError for file {filename_abs}: {e}", err=True)
                    self.warned_files.add(filename_abs)
                continue

            rel_filename = self._get_rel_filename(filename_abs)
            if len(Path(rel_filename).parts) <= self.max_tags_depth:
                file_tags = self._get_tags(filename_abs, rel_filename)
                file_def_tags = [tag for tag in file_tags if tag.kind == "def"]
                if file_def_tags:
                    all_tags.extend(file_def_tags)
                else:
                    all_tags.append((rel_filename,))
            else:
                all_tags.append((rel_filename,))

        return all_tags

    def get_tags_map(
        self,
        all_filenames_abs: List[str],  # All git-tracked files
        max_tokens: int,
        progress_callback: Optional[callable] = None,
    ) -> Optional[str]:
        if progress_callback:
            progress_callback(f"{REPO_MAP_MESSAGE}: Ranking tags and files...")
        tags = self._get_all_tags(
            all_filenames_abs=all_filenames_abs, progress_callback=progress_callback
        )

        # Prioritize "special" files (e.g. README) from all_filenames_abs
        # These are added at the beginning of the ranked list if not already effectively there.
        all_rel_filenames = sorted(
            list(set(self._get_rel_filename(filename) for filename in all_filenames_abs))
        )

        # Get relative paths of files already represented by ranked tags
        tags_rel_filenames = set()
        for tag in tags:
            if isinstance(tag, Tag):
                tags_rel_filenames.add(tag.rel_filename)
            else:
                tags_rel_filenames.add(tag[0])

        # Identify special files not already covered by high-ranking tags
        # These are added as (rel_filename,) tuples to ensure their presence.
        special_file_tuples_to_prepend = []
        # Note: filter_important_files returns list of rel_filenames
        for special_rel_filename in self._filter_important_files(
            all_rel_filenames
        ):  # Use all_rel_filenames
            if special_rel_filename not in tags_rel_filenames and special_rel_filename:
                special_file_tuples_to_prepend.append((special_rel_filename,))

        # Prepend special files, then the ranked list
        # This ensures special files are considered early in the truncation process.
        final_item_list = special_file_tuples_to_prepend + tags

        # Deduplicate while preserving order (important for prepended special files)
        # An item can be Tag or Tuple[str]. Need a consistent way to check uniqueness.
        # Uniqueness check: For Tags, by (rel_filename, name, line, kind). For Tuples,
        # by (rel_filename,).
        seen_items_repr = set()
        deduplicated_final_item_list = []
        for item in final_item_list:
            if isinstance(item, Tag):
                repr_key = (
                    "Tag",
                    item.rel_filename,
                    item.name,
                )  # Simpler key for deduplication
            else:  # Tuple (rel_filename,)
                repr_key = ("File", item[0])

            if repr_key not in seen_items_repr:
                deduplicated_final_item_list.append(item)
                seen_items_repr.add(repr_key)

        final_item_list = deduplicated_final_item_list

        # Binary search to find the number of items (tags or files) that fit token limit
        num_total_items = len(final_item_list)
        if num_total_items == 0:
            return ""  # Empty map if no items

        lower_bound = 0
        upper_bound = num_total_items
        best_map_text = ""
        best_map_tokens = 0  # Tokens of the best map found so far that is <= max_tokens

        # Iterative refinement (binary search like) to select items fitting token budget
        # Max iterations to prevent infinite loops with tricky token counts
        max_iterations = 20  # Should be enough for a wide range of num_total_items
        current_iter = 0

        # Heuristic for initial guess of items (middle)
        # Average tokens per item is unknown. Start with a fraction of total items.
        # Aider used `min(int(max_tokens // 25), num_tags)`. 25 is empirical avg tokens/tag line.
        middle = min(
            num_total_items, max(1, int(max_tokens / 25))
        )  # Ensure middle >= 1 if num_total_items > 0

        while lower_bound <= upper_bound and current_iter < max_iterations:
            current_iter += 1
            if progress_callback:
                progress_callback(
                    f"Formatting map, trying {middle} items (bounds: {lower_bound}-{upper_bound})"
                )

            current_selection = final_item_list[:middle]
            map_text = self.to_tree(current_selection)
            num_tokens = self._token_count(map_text)

            # Percentage error from target token count
            # Accept if within a certain tolerance (e.g., 15% of max_tokens)
            # This helps converge faster if an exact match is hard.
            token_err_pct = abs(num_tokens - max_tokens) / max_tokens if max_tokens > 0 else 0.0
            err_tolerance = 0.15

            if num_tokens <= max_tokens:  # Current map fits
                if num_tokens > best_map_tokens:  # And it's better than previous best
                    best_map_text = map_text
                    best_map_tokens = num_tokens

                if token_err_pct < err_tolerance:  # Good enough, stop
                    break
                lower_bound = middle + 1  # Try to include more items
            else:  # Current map is too large
                upper_bound = middle - 1  # Need to include fewer items

            if lower_bound > upper_bound:  # Bounds crossed
                break

            middle = (lower_bound + upper_bound) // 2
            if middle == 0 and lower_bound == 0 and upper_bound == 0 and num_total_items > 0:
                # If stuck at 0 but there are items, try at least 1 if map was too large
                if num_tokens > max_tokens:
                    middle = 0  # Stay at 0 if even 0 items (empty map) is too large (prefix issue?)
                else:
                    middle = 1  # Try 1 item if 0 items fit (empty map)

        if progress_callback:
            progress_callback("Map formatting complete.")
        return best_map_text

    @staticmethod
    def render_tree(
        abs_filename_str: str, rel_filename_str: str, lines_of_interest: List[int]
    ) -> str:
        """
        Renders a summarized view of a file, focusing on lines_of_interest.
        Uses grep_ast.TreeContext for structured formatting.
        """
        code = Path(abs_filename_str).read_text(encoding="utf-8", errors="ignore")
        if not code.endswith("\n"):
            code += "\n"  # Ensure trailing newline for TreeContext

        # TreeContext parameters from aider, adjusted for opsmith
        context = TreeContext(
            filename=rel_filename_str,
            code=code,
            color=False,
            line_number=False,
            child_context=False,
            last_line=False,
            margin=0,
            mark_lois=False,
            loi_pad=0,
            show_top_of_file_parent_scope=False,
        )

        # Set lines of interest for this specific rendering
        context.lines_of_interest = set(lines_of_interest)
        context.add_context()  # Compute context around LOIs

        formatted_text = context.format()
        return formatted_text

    def to_tree(self, items: List[Tag | Tuple[str]]) -> str:
        """
        Converts a list of Tags into a string tree representation.
        Tags are grouped by file.
        """
        if not items:
            return ""
        output_lines = []

        # Group tags by file to render each file's summary once
        file_to_tags: Dict[str, List[Tag]] = defaultdict(list)
        # Standalone files (rel_filename,) are processed separately
        standalone_files: List[str] = []

        for item in items:
            if isinstance(item, Tag):
                # item.filename is absolute path, item.rel_filename is relative
                file_to_tags[item.rel_filename].append(item)
            else:
                file_to_tags[item[0]] = []

        # Process files with tags
        # Sort by rel_filename for consistent output order
        for rel_filename_sorted, tags_in_file in sorted(file_to_tags.items(), key=lambda x: x[0]):
            if tags_in_file:
                output_lines.append(f"\n{rel_filename_sorted}:")
                # Need absolute path for render_tree to read file content
                # Assuming all tags in tags_in_file have the same abs_filename for this rel_filename
                abs_filename_for_render = tags_in_file[0].filename  # Get abs path from first tag

                lines_of_interest = [
                    tag.line for tag in tags_in_file if tag.line >= 0
                ]  # Valid line numbers
                rendered_content = self.render_tree(
                    abs_filename_for_render, rel_filename_sorted, lines_of_interest
                )
                output_lines.append(rendered_content)
            else:
                output_lines.append(f"\n{rel_filename_sorted}\n")

        # Process standalone files (those without specific tags to show, just list the filename)
        # These are files that were ranked but either had no tags or their tags weren't high enough.
        # Ensure they are not already covered by file_to_tags processing.
        processed_standalone_files = set(file_to_tags.keys())
        for rel_fname_standalone in sorted(list(set(standalone_files))):  # Sort and unique
            if rel_fname_standalone not in processed_standalone_files:
                output_lines.append(f"\n{rel_fname_standalone}")  # Just list filename

        full_output = "".join(output_lines)

        # Truncate very long lines (e.g., minified JS) as a final safety measure
        # This was in aider's original to_tree.
        truncated_lines = [line[:100] for line in full_output.splitlines()]
        return "\n".join(truncated_lines) + "\n"
