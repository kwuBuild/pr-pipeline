"""Fresh, attributed GitHub discussion data. Never a source of control records."""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from typing import Any

SOURCE_FIELDS = 'id url title body author { login } createdAt updatedAt'
COMMENT_FIELDS = 'id url body author { login } createdAt updatedAt'
REVIEW_FIELDS = COMMENT_FIELDS + ' submittedAt state commit { oid }'
THREAD_FIELDS = '''id path line originalLine startLine originalStartLine
    diffSide startDiffSide isResolved isOutdated resolvedBy { login }'''
INLINE_FIELDS = COMMENT_FIELDS + ''' path line originalLine diffHunk
    commit { oid } originalCommit { oid } replyTo { id }'''
REFERENCE_FIELDS = 'number repository { name owner { login } }'


class ContextError(ValueError):
    """A required source is unavailable, malformed, or incomplete."""


def repository_name(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r'[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}/[A-Za-z0-9._-]+', value,
    ) or value.split('/')[-1] in {'.', '..'}:
        raise ContextError('invalid repository identity')
    return value


def qualified_references(refs: object) -> list[tuple[str, int]]:
    """Validate #33's repository and positive-number contract before any read."""
    if not isinstance(refs, list):
        raise ContextError('closingIssuesReferences is not a list')
    result = []
    for ref in refs:
        if not isinstance(ref, dict):
            raise ContextError('invalid closing issue reference')
        number, repo = ref.get('number'), ref.get('repository')
        if type(number) is not int or number <= 0 or not isinstance(repo, dict):
            raise ContextError('invalid closing issue identity')
        owner = repo.get('owner')
        if not isinstance(owner, dict) or not isinstance(owner.get('login'), str) or not isinstance(repo.get('name'), str):
            raise ContextError('missing closing issue repository')
        result.append((repository_name(owner['login'] + '/' + repo['name']), number))
    return result


def governing_issues(refs: object) -> list[tuple[str, int]]:
    """Keep GitHub's linked issues in order, deduplicated by qualified identity."""
    identities = qualified_references(refs)
    seen = set()
    result = []
    for repo, number in identities:
        key = (repo.lower(), number)
        if key not in seen:
            result.append((repo, number))
            seen.add(key)
    return result


def request(read: Callable[..., Any], query: str, variables: dict[str, object], source: str) -> dict:
    args = ['api', 'graphql', '--hostname', 'github.com', '-f', f'query={query}']
    for key, value in variables.items():
        args.extend(['-F' if type(value) is int else '-f', f'{key}={value}'])
    try:
        response = read(*args)
    except (subprocess.CalledProcessError, ValueError, OSError) as exc:
        detail = getattr(exc, 'stderr', None) or str(exc)
        raise ContextError(f'{source}: {detail}') from exc
    if not isinstance(response, dict):
        raise ContextError(f'{source}: invalid GraphQL response')
    if response.get('errors'):
        raise ContextError(f'{source}: GraphQL errors: {json.dumps(response["errors"])}')
    if not isinstance(response.get('data'), dict):
        raise ContextError(f'{source}: missing GraphQL data')
    return response['data']


def attributed(record: object, source: str, *, title: bool = False) -> dict:
    if not isinstance(record, dict):
        raise ContextError(f'{source}: missing source')
    fields = ('id', 'url', 'createdAt', 'updatedAt') + (('title',) if title else ())
    for field in fields:
        if not isinstance(record.get(field), str) or not record[field]:
            raise ContextError(f'{source}: missing or invalid {field}')
    if 'body' not in record or (record['body'] is not None and not isinstance(record['body'], str)):
        raise ContextError(f'{source}: missing or invalid body')
    if 'author' not in record or (record['author'] is not None and (
        not isinstance(record['author'], dict) or not isinstance(record['author'].get('login'), str)
    )):
        raise ContextError(f'{source}: missing or invalid author')
    return {**record, 'body': record['body'] or ''}


def source_record(read: Callable[..., Any], repo: str, number: int, kind: str) -> dict:
    owner, name = repository_name(repo).split('/')
    query = f'''query Source($owner: String!, $name: String!, $number: Int!) {{
      repository(owner: $owner, name: $name) {{ {kind}(number: $number) {{ {SOURCE_FIELDS} }} }}
    }}'''
    label = f'{repo} {kind} #{number}'
    data = request(read, query, dict(owner=owner, name=name, number=number), label)
    repository = data.get('repository')
    return attributed(repository.get(kind) if isinstance(repository, dict) else None, label, title=True)


def pages(read: Callable[..., Any], identity: str, kind: str, field: str, fields: str) -> list[dict]:
    """Paginate one connection independently, including nested thread replies."""
    query = f'''query DiscussionPage($id: ID!, $cursor: String) {{
      node(id: $id) {{ ... on {kind} {{
        {field}(first: 100, after: $cursor) {{ nodes {{ {fields} }}
          pageInfo {{ hasNextPage endCursor }} }}
      }} }}
    }}'''
    result, seen = [], set()
    variables: dict[str, object] = {'id': identity}
    while True:
        label = f'{kind} {identity} {field} after {variables.get("cursor", "start")}'
        data = request(read, query, variables, label)
        node = data.get('node')
        connection = node.get(field) if isinstance(node, dict) else None
        if not isinstance(connection, dict) or not isinstance(connection.get('nodes'), list):
            raise ContextError(f'{label}: missing connection/nodes')
        info = connection.get('pageInfo')
        if not isinstance(info, dict) or type(info.get('hasNextPage')) is not bool or 'endCursor' not in info:
            raise ContextError(f'{label}: missing pageInfo')
        if info['endCursor'] is not None and not isinstance(info['endCursor'], str):
            raise ContextError(f'{label}: invalid endCursor')
        if any(not isinstance(item, dict) for item in connection['nodes']):
            raise ContextError(f'{label}: malformed node')
        result.extend(connection['nodes'])
        if not info['hasNextPage']:
            return result
        cursor = info['endCursor']
        if not isinstance(cursor, str) or not cursor or cursor in seen or not connection['nodes']:
            raise ContextError(f'{label}: pagination did not advance')
        seen.add(cursor)
        variables['cursor'] = cursor


def comments(read: Callable[..., Any], identity: str, kind: str, fields: str = COMMENT_FIELDS) -> list[dict]:
    records = [attributed(item, f'{kind} {identity} comment')
               for item in pages(read, identity, kind, 'comments', fields)]
    return sorted(records, key=lambda item: item['createdAt'])


def nullable_object(record: dict, key: str, child: str, source: str) -> None:
    value = record.get(key)
    if key not in record or (value is not None and (
        not isinstance(value, dict) or not isinstance(value.get(child), str) or not value[child]
    )):
        raise ContextError(f'{source}: invalid {key}')


def nullable_line(record: dict, key: str, source: str) -> None:
    value = record.get(key)
    if key not in record or (value is not None and (type(value) is not int or value <= 0)):
        raise ContextError(f'{source}: invalid {key}')


def readable(data: Any) -> Any:
    """Split body text into lossless parts that ordinary line readers can read.

    JSON escapes newlines inside a string, so one large comment otherwise
    becomes one enormous line. Joining the parts without a separator recovers
    the exact text, including whitespace and Unicode. No engine tool grant is
    needed to decode or slice an oversized line.
    """
    if isinstance(data, list):
        return [readable(item) for item in data]
    if isinstance(data, dict):
        return {
            key + 'Parts' if key in {'body', 'diffHunk'} else key:
            [value[start:start + 256] for start in range(0, len(value), 256)]
            if key in {'body', 'diffHunk'} else readable(value)
            for key, value in data.items()
        }
    return data


def acquire(read: Callable[..., Any], repo: str, number: int,
            primary_issue: Callable[[str], int]) -> dict:
    """Read all required sources afresh; never return an incomplete acquisition."""
    pr = source_record(read, repo, number, 'pullRequest')
    refs = pages(read, pr['id'], 'PullRequest', 'closingIssuesReferences', REFERENCE_FIELDS)
    identities = governing_issues(refs)
    # GitHub omits closing links for stacked PRs. Reuse the existing primary
    # issue contract only when there is no reported association.
    if not identities and (primary := primary_issue(pr['body'])):
        identities = [(repo, primary)]
    issues = []
    for issue_repo, issue_number in identities:
        issue = source_record(read, issue_repo, issue_number, 'issue')
        issue.update(repository=issue_repo, number=issue_number)
        issue['comments'] = comments(read, issue['id'], 'Issue')
        issues.append(issue)
    pr['comments'] = comments(read, pr['id'], 'PullRequest')
    reviews = []
    for item in pages(read, pr['id'], 'PullRequest', 'reviews', REVIEW_FIELDS):
        item = attributed(item, f'{repo} PR #{number} review')
        if not isinstance(item.get('state'), str) or 'submittedAt' not in item or 'commit' not in item:
            raise ContextError('review: missing state/submission/commit metadata')
        if item['state'] == 'PENDING':
            continue  # Unsubmitted drafts are not part of the public discussion.
        if not isinstance(item['submittedAt'], str):
            raise ContextError('review: missing submittedAt')
        nullable_object(item, 'commit', 'oid', 'review')
        reviews.append(item)
    pr['reviews'] = sorted(reviews, key=lambda item: item['submittedAt'])
    threads = pages(read, pr['id'], 'PullRequest', 'reviewThreads', THREAD_FIELDS)
    for thread in threads:
        for key in ('id', 'path'):
            if not isinstance(thread.get(key), str) or not thread[key]:
                raise ContextError(f'review thread: missing {key}')
        for key in ('isResolved', 'isOutdated'):
            if type(thread.get(key)) is not bool:
                raise ContextError(f'review thread: missing {key}')
        for key in ('line', 'originalLine', 'startLine', 'originalStartLine', 'diffSide', 'startDiffSide', 'resolvedBy'):
            if key not in thread:
                raise ContextError(f'review thread: missing {key}')
        for key in ('line', 'originalLine', 'startLine', 'originalStartLine'):
            nullable_line(thread, key, 'review thread')
        nullable_object(thread, 'resolvedBy', 'login', 'review thread')
        if thread['diffSide'] not in ('LEFT', 'RIGHT') or thread['startDiffSide'] not in (None, 'LEFT', 'RIGHT'):
            raise ContextError('review thread: invalid diff side')
        thread['comments'] = comments(read, thread['id'], 'PullRequestReviewThread', INLINE_FIELDS)
        for comment in thread['comments']:
            for key in ('path', 'line', 'originalLine', 'diffHunk', 'commit', 'originalCommit', 'replyTo'):
                if key not in comment:
                    raise ContextError(f'inline comment: missing {key}')
            for key in ('line', 'originalLine'):
                nullable_line(comment, key, 'inline comment')
            for key, child in (('commit', 'oid'), ('originalCommit', 'oid'), ('replyTo', 'id')):
                nullable_object(comment, key, child, 'inline comment')
            if not isinstance(comment['path'], str) or not isinstance(comment['diffHunk'], str):
                raise ContextError('inline comment: invalid path/diffHunk')
    pr['reviewThreads'] = threads
    pr.update(repository=repo, number=number)
    return {'pr': pr, 'issues': issues}
