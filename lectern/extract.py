__all__ = [
    "Extractor",
    "TextExtractor",
    "EmbeddedExtractor",
    "MarkdownExtractor",
]


import re
from itertools import islice
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Tuple, Union
from urllib.parse import unquote

from beet import Cache, DataPack, ResourcePack
from beet.core.utils import FileSystemPath
from markdown_it import MarkdownIt
from markdown_it.common import normalize_url
from markdown_it.token import Token

from .directive import Directive
from .fragment import Fragment
from .utils import is_path

# Patch markdown_it to allow arbitrary data urls
# https://github.com/executablebooks/markdown-it-py/issues/128
# TODO: Will soon be able to use custom validateLink
normalize_url.GOOD_DATA_RE = re.compile(r"^data:")


class Extractor:
    """Base class for extractors."""

    directives: Dict[str, Directive]
    regex: Optional["re.Pattern[str]"]
    cache: Optional[Cache]

    def __init__(self, cache: Optional[Cache] = None):
        self.directives = {}
        self.regex = None
        self.cache = cache

    def generate_regex(self) -> str:
        """Return a regex that can match the current directives."""
        names = "|".join(name for name in self.directives)
        modifier = r"(?:\((?P<modifier>[^)]*)\)|\b)"
        arguments = r"(?P<arguments>.*)"
        return f"@(?P<name>{names}){modifier}{arguments}"

    def compile_regex(self, regex: str) -> "re.Pattern[str]":
        """Return the compiled pattern for the directive regex."""
        return re.compile(f"^{regex}$", flags=re.MULTILINE)

    def get_regex(self, directives: Mapping[str, Directive]) -> "re.Pattern[str]":
        """Create and return the regex for the specified directives."""
        directives = dict(directives)

        if self.regex is None or self.directives != directives:
            self.directives = directives
            self.regex = self.compile_regex(self.generate_regex())

        return self.regex

    def extract(
        self,
        source: str,
        directives: Mapping[str, Directive],
    ) -> Tuple[ResourcePack, DataPack]:
        """Extract a resource pack and a data pack."""
        return self.apply_directives(
            directives, self.parse_fragments(source, directives)
        )

    def apply_directives(
        self,
        directives: Mapping[str, Directive],
        fragments: Iterable[Fragment],
    ) -> Tuple[ResourcePack, DataPack]:
        """Apply directives into a blank data pack and a blank resource pack."""
        assets, data = ResourcePack(), DataPack()

        for fragment in fragments:
            directives[fragment.directive](fragment, assets, data)

        return assets, data

    def parse_fragments(
        self,
        source: str,
        directives: Mapping[str, Directive],
    ) -> Iterator[Fragment]:
        """Parse and yield pack fragments."""
        return iter([])

    def create_fragment(
        self,
        match: "re.Match[str]",
        content: Optional[str] = None,
        url: Optional[str] = None,
        path: Optional[FileSystemPath] = None,
    ):
        """Helper for creating a fragment from a matched pattern."""
        directive, modifier, arguments = match.groups()
        return Fragment(
            directive=directive,
            modifier=modifier,
            arguments=arguments.split(),
            content=content,
            url=url,
            path=path,
            cache=self.cache,
        )


class TextExtractor(Extractor):
    """Extractor for plain text files."""

    def parse_fragments(
        self,
        source: str,
        directives: Mapping[str, Directive],
    ) -> Iterator[Fragment]:
        tokens = self.get_regex(directives).split(source + "\n")

        it = iter(tokens)
        next(it)

        while True:
            try:
                directive, modifier, arguments, content = islice(it, 4)
            except ValueError:
                return
            else:
                content = content.partition("\n")[-1]
                yield Fragment(
                    directive=directive,
                    modifier=modifier,
                    arguments=arguments.split(),
                    content=content[:-1] if content.endswith("\n") else content,
                    cache=self.cache,
                )


class EmbeddedExtractor(TextExtractor):
    """Extractor for directives embedded in markdown code blocks."""

    def generate_regex(self) -> str:
        return r"(?://|#)\s*" + super().generate_regex()


class MarkdownExtractor(Extractor):
    """Extractor for markdown files."""

    embedded_extractor: TextExtractor
    parser: MarkdownIt
    html_comment_regex: "re.Pattern[str]"

    def __init__(self, cache: Optional[Cache] = None):
        super().__init__(cache)
        self.embedded_extractor = EmbeddedExtractor(cache)
        self.parser = MarkdownIt()
        self.html_comment_regex = re.compile(r"<!--\s*(.+?)\s*-->")

    def extract(
        self,
        source: str,
        directives: Mapping[str, Directive],
        external_files: Optional[FileSystemPath] = None,
    ) -> Tuple[ResourcePack, DataPack]:
        return self.apply_directives(
            directives, self.parse_fragments(source, directives, external_files)
        )

    def parse_fragments(
        self,
        source: str,
        directives: Mapping[str, Directive],
        external_files: Optional[FileSystemPath] = None,
    ) -> Iterator[Fragment]:
        tokens = self.parser.parse(source)  # type: ignore
        regex = self.get_regex(directives)

        skip = 1

        for i, token in enumerate(tokens):
            if skip > 1:
                skip -= 1
                continue

            #
            # `@directive args...`
            #
            # ```
            # content
            # ```
            #
            if (
                (
                    skip := self.match_tokens(
                        tokens[i : i + 4],
                        "paragraph_open",
                        "inline",
                        "paragraph_close",
                        ["fence", "code_block"],
                    )
                )
                and (inline := tokens[i + 1])
                and inline.children
                and self.match_tokens(inline.children, "code_inline")
                and (match := regex.match(inline.children[0].content))
            ):
                yield self.create_fragment(match, content=tokens[i + 3].content)

            #
            # `@directive args...`
            #
            # ![](path/to/image)
            #
            elif (
                (
                    skip := self.match_tokens(
                        tokens[i : i + 6],
                        "paragraph_open",
                        "inline",
                        "paragraph_close",
                        "paragraph_open",
                        "inline",
                        "paragraph_close",
                    )
                )
                and (inline := tokens[i + 1])
                and inline.children
                and self.match_tokens(inline.children, "code_inline")
                and (image := tokens[i + 4])
                and image.children
                and self.match_tokens(image.children, "image")
                and (link := image.children[0].attrGet("src"))
                and (match := regex.match(inline.children[0].content))
            ):
                yield self.create_link_fragment(match, link, external_files)

            #
            # `@directive args...`
            #
            # <details>
            #
            # ```
            # content
            # ```
            #
            # </details>
            #
            elif (
                (
                    skip := self.match_tokens(
                        tokens[i : i + 6],
                        "paragraph_open",
                        "inline",
                        "paragraph_close",
                        "html_block",
                        ["fence", "code_block"],
                        "html_block",
                    )
                )
                and (inline := tokens[i + 1])
                and inline.children
                and self.match_tokens(inline.children, "code_inline")
                and tokens[i + 3].content == "<details>\n"
                and tokens[i + 5].content == "</details>\n"
                and (match := regex.match(inline.children[0].content))
            ):
                yield self.create_fragment(match, content=tokens[i + 4].content)

            #
            # `@directive args...`
            #
            # <details>
            #
            # ![](path/to/image)
            #
            # </details>
            #
            elif (
                (
                    skip := self.match_tokens(
                        tokens[i : i + 8],
                        "paragraph_open",
                        "inline",
                        "paragraph_close",
                        "html_block",
                        "paragraph_open",
                        "inline",
                        "paragraph_close",
                        "html_block",
                    )
                )
                and (inline := tokens[i + 1])
                and inline.children
                and self.match_tokens(inline.children, "code_inline")
                and tokens[i + 3].content == "<details>\n"
                and tokens[i + 7].content == "</details>\n"
                and (image := tokens[i + 5])
                and image.children
                and self.match_tokens(image.children, "image")
                and (link := image.children[0].attrGet("src"))
                and (match := regex.match(inline.children[0].content))
            ):
                yield self.create_link_fragment(match, link, external_files)

            #
            # [`@directive args...`](path/to/content)
            #
            elif (
                (
                    skip := self.match_tokens(
                        tokens[i : i + 3],
                        "paragraph_open",
                        "inline",
                        "paragraph_close",
                    )
                )
                and (inline := tokens[i + 1])
                and inline.children
                and self.match_tokens(
                    inline.children,
                    "link_open",
                    "code_inline",
                    "link_close",
                )
                and (link := inline.children[0].attrGet("href"))
                and (match := regex.match(inline.children[1].content))
            ):
                yield self.create_link_fragment(match, link, external_files)

            #
            # <!-- @directive args... -->
            #
            # ```
            # content
            # ```
            #
            elif (
                (
                    skip := self.match_tokens(
                        tokens[i : i + 2],
                        "html_block",
                        ["fence", "code_block"],
                    )
                )
                and (comment := self.html_comment_regex.match(token.content))
                and (match := regex.match(comment.group(1)))
            ):
                yield self.create_fragment(match, content=tokens[i + 1].content)

            #
            # <!-- @directive args... -->
            #
            # ![](path/to/image)
            #
            elif (
                (
                    skip := self.match_tokens(
                        tokens[i : i + 4],
                        "html_block",
                        "paragraph_open",
                        "inline",
                        "paragraph_close",
                    )
                )
                and (comment := self.html_comment_regex.match(token.content))
                and (image := tokens[i + 2])
                and image.children
                and self.match_tokens(image.children, "image")
                and (link := image.children[0].attrGet("src"))
                and (match := regex.match(comment.group(1)))
            ):
                yield self.create_link_fragment(match, link, external_files)

            #
            # `@directive args...`
            #
            elif (
                (
                    skip := self.match_tokens(
                        tokens[i : i + 3],
                        "paragraph_open",
                        "inline",
                        "paragraph_close",
                    )
                )
                and (inline := tokens[i + 1])
                and inline.children
                and self.match_tokens(inline.children, "code_inline")
                and (match := regex.match(inline.children[0].content))
            ):
                yield self.create_fragment(match)

            #
            # <!-- @directive args... -->
            #
            elif (
                token.type == "html_block"
                and (comment := self.html_comment_regex.match(token.content))
                and (match := regex.match(comment.group(1)))
            ):
                yield self.create_fragment(match)

            #
            # ```
            # @directive args...
            #
            # content
            # ```
            #
            elif token.type in ["fence", "code_block"]:
                yield from self.embedded_extractor.parse_fragments(
                    token.content,
                    directives,
                )

    def match_tokens(
        self,
        tokens: Optional[List[Token]],
        *token_types: Union[List[str], str],
    ) -> int:
        """Return whether the list of tokens matches the provided token types."""
        return (
            tokens is not None
            and len(tokens) == len(token_types)
            and all(
                (
                    token_type == token.type
                    if isinstance(token_type, str)
                    else token.type in token_type
                )
                for token, token_type in zip(tokens, token_types)
            )
            and len(tokens)
        )

    def create_link_fragment(
        self,
        match: "re.Match[str]",
        link: str,
        external_files: Optional[FileSystemPath] = None,
    ) -> Fragment:
        """Helper for creating a fragment from a link."""
        url = unquote(link)  # TODO: Will soon be able to use custom normalizeLink
        path = None

        if is_path(url):
            if external_files:
                path = Path(external_files, url).resolve()
            url = None

        return self.create_fragment(match, url=url, path=path)
