"""
All things query
"""
from math import ceil
from re import finditer
from typing import Mapping, Iterable, Optional, Union, Dict

from .elastic import es


def build_body(queries: Mapping[str, str] = None, filters: Mapping = None, highlight: bool = False):
    def parse_filter(field, filter):
        field_filters = []
        for value in filter.pop('values', []):
            field_filters.append({"term": {field: value}})
        if 'value' in filter:
            field_filters.append({"term": {field: filter.pop('value')}})

        rangefilter = {}
        for rangevar in ['gt', 'gte', 'lt', 'lte']:
            if rangevar in filter:
                rangefilter[rangevar] = filter.pop(rangevar)
        if rangefilter:
            field_filters.append({"range": {field: rangefilter}})
        if filter:
            raise ValueError(f"Unknown filter type(s): {filter}")
        return {'bool': {'should': field_filters}}

    def parse_query(q):
        return {"query_string": {"query":  q}}

    def parse_queries(qs):
        if len(qs.values()) == 1:
            return parse_query(list(qs.values())[0])
        else:
            return {"bool": {"should": [parse_query(q) for q in qs.values()]}}

    if not queries and not filters:
        return {'query': {'match_all': {}}}

    fs = [parse_filter(*item) for item in filters.items()] if filters else []
    if queries:
        fs.append(parse_queries(queries))

    body = {"query": {"bool": {"filter": fs}}}
    if highlight:
        body['highlight'] = {"type": 'plain', "fields": {"*": {"number_of_fragments": 0}}}
    return body


class QueryResult:
    def __init__(self, data, n=None, per_page=None, page=None, page_count=None, scroll_id=None):
        if n and page_count is None:
            page_count = ceil(n.get("value") / per_page)
        self.data = data
        self.total_count = n
        self.page = page
        self.page_count = page_count
        self.per_page = per_page
        self.scroll_id = scroll_id

    def as_dict(self):
        meta = {"total_count": self.total_count,
                "per_page": self.per_page,
                "page_count": self.page_count,
                }
        if self.scroll_id:
            meta['scroll_id'] = self.scroll_id
        else:
            meta['page'] = self.page
        return dict(meta=meta, results=self.data)


def query_documents(index: str, queries: Union[Dict[str,  str], Iterable[str]] = None, *,
                    page: int = 0, per_page: int = 10,
                    scroll=None, scroll_id: str = None, fields: Iterable[str] = None,
                    filters: Mapping[str, Mapping] = None,
                    highlight=False, annotations=False,
                    **kwargs) -> Optional[QueryResult]:
    """
    Conduct a query_string query, returning the found documents
    It will return at most per_page results.
    In normal (paginated) mode, the next batch can be  requested by incrementing the page parameter.
    If the scroll parameter is given, the result will contain a scroll_id which can be used to get the next batch.
    In case there are no more documents to scroll, it will return None
    :param index: The name of the index
    :param queries: a list of queries OR a dict {label1: query1, ...}
    :param page: The number of the page to request (starting from zero)
    :param per_page: The number of hits per page
    :param scroll: if not None, will create a scroll request rather than a paginated request. Parmeter should
                   specify the time the context should be kept alive, or True to get the default of 2m.
    :param scroll_id: if not None, should be a previously returned context_id to retrieve a new page of results
    :param fields: if not None, specify a list of fields to retrieve for each hit
    :param filters: if not None, a dict of filters with either value, values, or gte/gt/lte/lt ranges:
                       {field: {'values': [value1,value2],
                                'value': value,
                                'gte/gt/lte/lt': value,
                                ...}}
    :param highlight: if True, add highlight tags (<em>) to results.
    :param annotations: if True, get query matches as annotations.
    :param kwargs: Additional elements passed to Elasticsearch.search(), for example:
           sort=col1:desc,col2
    :return: a QueryResult, or None if there is not scroll result anymore
    """
    if scroll or scroll_id:
        # set scroll to default also if scroll_id is given but no scroll time is known
        kwargs['scroll'] = '2m' if (not scroll or scroll is True) else scroll

    if queries and not isinstance(queries, dict):
        queries = {q: q for q in queries}

    if scroll_id:
        result = es.scroll(scroll_id=scroll_id, **kwargs)
        if not result['hits']['hits']:
            return None
    else:
        body = build_body(queries, filters, highlight)

        if fields:
            fields = fields if isinstance(fields, list) else list(fields)
            kwargs['_source'] = fields
        if not scroll:
            kwargs['from_'] = page * per_page
        result = es.search(index=index, body=body, size=per_page, **kwargs)

    data = []
    for hit in result['hits']['hits']:
        hitdict = dict(_id=hit['_id'], **hit['_source'])
        if annotations:
            hitdict['_annotations'] = query_annotations(index, hit['_id'], queries)
        if 'highlight' in hit:
            for key in hit['highlight'].keys():
                if hit['highlight'][key]:
                    hitdict[key] = hit['highlight'][key][0]
        data.append(hitdict)

    if scroll_id:
        return QueryResult(data, scroll_id=scroll_id)
    elif scroll:
        return QueryResult(data, n=result['hits']['total'], per_page=per_page, scroll_id=result['_scroll_id'])
    else:
        return QueryResult(data, n=result['hits']['total'], per_page=per_page,  page=page)


def query_annotations(index: str, id: str, queries: Iterable[str]):
    """
    get query matches in annotation format. Currently does so per hit per query.
    Per hit could be optimized, but per query seems necessary:
    https://stackoverflow.com/questions/44621694/elasticsearch-highlight-with-multiple-queries-not-work-as-expected
    """
    annotations = []
    for query in queries:
        body = build_body([query], {'_id': {'value': id}}, True)

        result = es.search(index=index, body=body)
        hit = result['hits']['hits'][0]
        for field, highlights in hit['highlight'].items():
            for span in extract_highlight_span(highlights[0]):
                span['variable'] = 'lucene_query'
                span['value'] = query
                span['field'] = field
                annotations.append(span)
        return annotations


def extract_highlight_span(highlight):
    """
    It doesn't seem possible to get the offsets of highlights:
    https://github.com/elastic/elasticsearch/issues/5736

    We can get the offsets from the tags, but not yet sure how stable this is.
    """
    regex = '<em>.+</em>'
    tagsize = 9  # <em></em>
    for i, m in enumerate(finditer(regex, highlight)):
        offset = m.start(0) - tagsize*i
        length = len(m.group(0)) - tagsize
        yield dict(offset=offset, length=length)
