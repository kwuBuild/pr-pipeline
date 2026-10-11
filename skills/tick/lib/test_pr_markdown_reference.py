"""Optional development oracle: pip install pytest markdown-it-py, then run this file.

Production and the normal stdlib-only CI matrix do not depend on the renderer.
The mandatory CLI regressions live in test_pr_metadata.py.
"""

import re

import pytest

import tick

markdown_it = pytest.importorskip("markdown_it")


@pytest.fixture
def commonmark():
    return markdown_it.MarkdownIt("commonmark")


@pytest.mark.parametrize("prefix", [
    "Example paragraph", "`Example paragraph`", "#85 is the issue",
    "~Approximate example", "``inline code``", "####### not a heading",
    "```not `a fence`", "~~strikethrough~~", "    indented paragraph continuation",
    "### Real heading", "***", "- - -", "___", "Paragraph\n---",
    "Paragraph\n===", "Paragraph\n\n", "1. Previous item",
    "1. Previous item\n   More detail", "1. Previous item\n 2. Previous sibling",
])
@pytest.mark.parametrize("primary", [False, True])
@pytest.mark.parametrize("container", ["", "- "])
def test_reference_renderer_agrees_on_fence_authority(commonmark, prefix, primary, container):
    segment = prefix + "\n2. ```md\n   sample text\n   ```\n\nCloses #999\n"
    if container:
        segment = container + segment.replace("\n", "\n  ").rstrip() + "\n"
    body = "## Summary\n" + ("Closes #7\n" if primary else "") + "\n" + segment
    expected = set()
    for token in commonmark.parse(body):
        if token.type == "inline":
            # Only unformatted standalone directives in rendered document text
            # count. The parser has already removed list markers and code blocks.
            for match in re.finditer(r"(?m)^Closes #(\d+) *$", token.content):
                expected.add(match[1])
    assert tick._primary_closures(body) == expected, commonmark.render(body)


@pytest.mark.parametrize("text", [
    "    Closes #7\n", "\tCloses #7\n", "-     Closes #7\n",
    "1. Previous item\n2.     Closes #7\n", "- Previous item\n-     Closes #7\n",
    "Paragraph\n    Closes #7\n", "- Paragraph\n      Closes #7\n",
])
def test_reference_renderer_agrees_on_indented_code(commonmark, text):
    body = "## Summary\n\n" + text
    visible = any(token.type == "inline" and "Closes #7" in token.content
                  for token in commonmark.parse(body))
    assert bool(tick._primary_closures(body)) == visible, commonmark.render(body)


@pytest.mark.parametrize("example", [
    "- Example\n2. ```md\n   Closes #999\n   ```\n",
    "1. Example\n2) ```md\n   Closes #999\n   ```\n",
])
@pytest.mark.parametrize("container", ["", "- "])
def test_reference_renderer_agrees_on_changed_list_markers(commonmark, example, container):
    if container:
        example = container + example.replace("\n", "\n  ").rstrip() + "\n"
    body = "## Summary\nCloses #7\n\n" + example
    expected = set()
    for token in commonmark.parse(body):
        if token.type == "inline":
            expected.update(re.findall(r"(?m)^ *Closes #(\d+) *$", token.content))
    assert expected == {"7"}, commonmark.render(body)
    assert tick._primary_closures(body) == expected, commonmark.render(body)


@pytest.mark.parametrize("marker,indent", [("-", "  "), ("1.", "   ")])
@pytest.mark.parametrize("blank", ["", "\n", "\n\n"])
@pytest.mark.parametrize("container", ["", "- "])
def test_reference_renderer_agrees_on_empty_item_lifetime(commonmark, marker, indent, blank, container):
    example = marker + "\n" + blank + indent + "```md\nCloses #999\n```\n"
    if container:
        example = container + example.replace("\n", "\n  ").rstrip() + "\n"
    body = "## Summary\nCloses #7\n\n" + example
    expected = set()
    for token in commonmark.parse(body):
        if token.type == "inline":
            expected.update(re.findall(r"(?m)^ *Closes #(\d+) *$", token.content))
    assert expected == ({"7"} if blank else {"7", "999"}), commonmark.render(body)
    assert tick._primary_closures(body) == expected, commonmark.render(body)
