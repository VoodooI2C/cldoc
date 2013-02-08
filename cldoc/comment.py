from clang import cindex
from defdict import Defdict
from cldoc.struct import Struct

import os, re, sys, bisect

class Sorted(list):
    def __init__(self, key=None):
        if key is None:
            key = lambda x: x

        self.keys = []
        self.key = key

    def insert_bisect(self, item, bi):
        k = self.key(item)
        idx = bi(self.keys, k)

        self.keys.insert(idx, k)
        return super(Sorted, self).insert(idx, item)

    def insert(self, item):
        return self.insert_bisect(item, bisect.bisect_left)

    insert_left = insert

    def insert_right(self, item):
        return self.insert_bisect(item, bisect.bisect_right)

    def bisect(self, item, bi):
        k = self.key(item)

        return bi(self.keys, k)

    def bisect_left(self, item):
        return self.bisect(item, bisect.bisect_left)

    def bisect_right(self, item):
        return self.bisect(item, bisect.bisect_right)

    def find(self, key):
        i = bisect.bisect_left(self.keys, key)

        if i != len(self.keys) and self.keys[i] == key:
            return self[i]
        else:
            return None

class Comment(object):
    class String(object):
        def __init__(self, s):
            self.components = [s]

        def __str__(self):
            return "".join(self.components)

        def __nonzero__(self):
            l = len(self.components)

            return l > 0 and (l > 1 or len(self.components[0]) > 0)

    class UnresolvedReference(str):
        pass

    redocref = re.compile('(?P<isregex>[$]?)<(?P<ref>operator(?:>>|>|>=)|[^> ]+)>')
    redoccode = re.compile('^    \\[code\\]\n(?P<code>(?:(?:    .*|)\n)*)', re.M)

    def __init__(self, text, location):
        self.__dict__['docstrings'] = []
        self.__dict__['text'] = text

        self.__dict__['location'] = location
        self.__dict__['_resolved'] = False

        self.doc = text
        self.brief = ''

    def __setattr__(self, name, val):
        if not name in self.docstrings:
            self.docstrings.append(name)

        if isinstance(val, dict):
            for key in val:
                if not isinstance(val[key], Comment.String):
                    val[key] = Comment.String(str(val[key]))
        elif not isinstance(val, Comment.String):
            val = Comment.String(str(val))

        self.__dict__[name] = val

    def redoc_split(self, doc):
        ret = []
        lastpos = 0

        for m in Comment.redocref.finditer(doc):
            span = m.span(0)

            prefix = doc[lastpos:span[0]]
            lastpos = span[1]

            ret.append(prefix)
            ref = m.group('ref')

            if len(m.group('isregex')) > 0:
                ret.append(re.compile(ref))
            else:
                ret.append(ref)

        ret.append(doc[lastpos:])
        return ret

    def resolve_refs_for_doc(self, doc, resolver, root):
        components = self.redoc_split(str(doc))

        for i in range(1, len(components), 2):
            name = components[i]

            if isinstance(name, basestring):
                names = name.split('::')
            else:
                names = [name]

            nds = [root]

            for j in range(len(names)):
                newnds = []

                for n in nds:
                    newnds += resolver(n, names[j], j == 0)

                if len(newnds) == 0:
                    break

                nds = newnds

            if len(newnds) > 0:
                components[i] = newnds
            else:
                components[i] = Comment.UnresolvedReference(components[i])

        doc.components = components

    def resolve_refs(self, resolver, root):
        if self.__dict__['_resolved']:
            return

        self.__dict__['_resolved'] = True

        for name in self.docstrings:
            doc = getattr(self, name)

            if not doc:
                continue

            if isinstance(doc, dict):
                for key in doc:
                    if not isinstance(doc[key], Comment.String):
                        doc[key] = Comment.String(str(doc[key]))

                    self.resolve_refs_for_doc(doc[key], resolver, root)
            else:
                self.resolve_refs_for_doc(doc, resolver, root)

class RangeMap(Sorted):
    Item = Struct.define('Item', obj=None, start=0, end=0)

    def __init__(self):
        super(RangeMap, self).__init__(key=lambda x: x.start)

        self.stack = []

    def push(self, obj, start):
        self.stack.append(RangeMap.Item(obj=obj, start=start, end=start))

    def pop(self, end):
        item = self.stack.pop()
        item.end = end

        self.insert(item)

    def insert(self, item, start=None, end=None):
        if not isinstance(item, RangeMap.Item):
            item = RangeMap.Item(obj=item, start=start, end=end)

        self.insert_right(item)

    def find(self, i):
        # Finds object for which i falls in the range of that object
        idx = bisect.bisect_right(self.keys, i)

        # Go back up until falls within end
        while idx > 0:
            idx -= 1

            o = self[idx]

            if i <= o.end:
                return o.obj

        return None

class CommentsDatabase(object):
    cldoc_instrre = re.compile('^cldoc:([a-zA-Z_-]+)(\(([^\)]*)\))?')

    def __init__(self, filename, tu):
        self.filename = filename

        self.categories = RangeMap()
        self.comments = Sorted(key=lambda x: x.location.offset)

        self.extract(filename, tu)

    def parse_cldoc_instruction(self, token, s):
        m = CommentsDatabase.cldoc_instrre.match(s)

        if not m:
            return False

        func = m.group(1)
        args = m.group(3)

        if args:
            args = [x.strip() for x in args.split(",")]
        else:
            args = []

        name = 'cldoc_instruction_{0}'.format(func.replace('-', '_'))

        if hasattr(self, name):
            getattr(self, name)(token, args)
        else:
            sys.stderr.write('Invalid cldoc instruction: {0}\n'.format(func))
            sys.exit(1)

        return True

    @property
    def category_names(self):
        for item in self.categories:
            yield item.obj

    def location_to_str(self, loc):
        return '{0}:{1}:{2}'.format(loc.file.name, loc.line, loc.column)

    def cldoc_instruction_begin_category(self, token, args):
        if len(args) != 1:
            sys.stderr.write('No category name specified (at {0})\n'.format(self.location_to_str(token.location)))

            sys.exit(1)

        category = args[0]
        self.categories.push(category, token.location.offset)

    def cldoc_instruction_end_category(self, token, args):
        if len(self.categories.stack) == 0:
            sys.stderr.write('Failed to end cldoc category: no category to end (at {0})\n'.format(self.location_to_str(token.location)))

            sys.exit(1)

        last = self.categories.stack[-1]

        if len(args) == 1 and last.obj != args[0]:
            sys.stderr.write('Failed to end cldoc category: current category is `{0}\', not `{1}\' (at {2})\n'.format(last.obj, args[0], self.location_to_str(token.location)))

            sys.exit(1)

        self.categories.pop(token.extent.end.offset)

    def lookup_category(self, location):
        if location.file.name != self.filename:
            return None

        return self.categories.find(location.offset)

    def lookup(self, location):
        if location.file.name != self.filename:
            return None

        return self.comments.find(location.offset)

    def extract(self, filename, tu):
        """
        extract extracts comments from a translation unit for a given file by
        iterating over all the tokens in the TU, locating the COMMENT tokens and
        finding out to which cursors the comments semantically belong.
        """
        it = tu.get_tokens(extent=tu.get_extent(filename, (0, os.stat(filename).st_size)))

        while True:
            try:
                self.extract_loop(it)
            except StopIteration:
                break

    def extract_loop(self, iter):
        token = iter.next()

        # Skip until comment found
        while token.kind != cindex.TokenKind.COMMENT:
            token = iter.next()

        comments = []

        # Concatenate individual comments together
        while token.kind == cindex.TokenKind.COMMENT:
            comments.append(self.clean(token))
            token = iter.next()

        s = "\n".join(comments)

        # Parse special cldoc:<instruction>() comments for instructions
        if self.parse_cldoc_instruction(token, s.strip()):
            return

        comment = Comment(s, token.location)
        self.comments.insert(comment)

    def clean(self, token):
        prelen = token.extent.start.column - 1
        comment = token.spelling.strip()

        if comment.startswith('//'):
            return comment[2:].strip()
        elif comment.startswith('/*') and comment.endswith('*/'):
            lines = comment[2:-2].splitlines()
            retl = []

            for line in lines:
                if prelen == 0 or line[0:prelen].isspace():
                    line = line[prelen:].rstrip()

                    if line.startswith(' *') or line.startswith('  '):
                        line = line[2:]

                        if len(line) > 0 and line[0] == ' ':
                            line = line[1:]

                retl.append(line)

            return "\n".join(retl)
        else:
            return comment

from pyparsing import *

class Parser:
    ParserElement.setDefaultWhitespaceChars(' \t\r')

    identifier = Word(alphas + '_', alphanums + '_')

    brief = restOfLine.setResultsName('brief') + lineEnd

    paramdesc = restOfLine + ZeroOrMore(lineEnd + ~('@' | lineEnd) + Regex('[^\n]+')) + lineEnd.suppress()
    param = '@' + identifier.setResultsName('name') + White() + Combine(paramdesc).setResultsName('description')

    preparams = ZeroOrMore(param.setResultsName('preparam', listAllMatches=True))
    postparams = ZeroOrMore(param.setResultsName('postparam', listAllMatches=True))

    bodyline = NotAny('@') + (lineEnd | (Regex('[^\n]+') + lineEnd))
    body = ZeroOrMore(lineEnd) + Combine(ZeroOrMore(bodyline)).setResultsName('body')

    doc = brief + preparams + body + postparams

    @staticmethod
    def parse(s):
        return Parser.doc.parseString(s)

# vi:ts=4:et
