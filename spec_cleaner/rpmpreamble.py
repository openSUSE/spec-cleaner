# vim: set ts=4 sw=4 et: coding=UTF-8

import re

from .rpmsection import Section
from .rpmexception import RpmException
from .rpmpreambleelements import RpmPreambleElements
from .dependency_parser import DependencyParser
from .rpmhelpers import fix_license

class RpmPreamble(Section):

    """
        Only keep one empty line for many consecutive ones.
        Reorder lines.
        Fix bad licenses.
        Use one line per BuildRequires/Requires/etc.
        Standardize BuildRoot.

        This one is a bit tricky since we reorder things. We have a notion of
        paragraphs, categories, and groups.

        A paragraph is a list of non-empty lines. Conditional directives like
        %if/%else/%endif also mark paragraphs. It contains categories.
        A category is a list of lines on the same topic. It contains a list of
        groups.
        A group is a list of lines where the first few ones are comment lines,
        and the last one is a normal line.

        This means that comments will stay attached to one
        line, even if we reorder the lines.
    """

    def __init__(self, options):
        Section.__init__(self, options)
        # Old storage
        self._oldstore = []
        # Is the parsed variable multiline (ending with \)
        self.multiline = False
        # Are we inside of conditional or not
        self.condition = False
        # Is the condition with define/global variables
        self._condition_define = False
        # Is the condition based probably on bcond evaluation
        self._condition_bcond = False
        self.options = options
        # do we want pkgconfig and others?
        self.pkgconfig = options['pkgconfig']
        self.perl = options['perl']
        self.cmake = options['cmake']
        self.tex = options['tex']
        # are we supposed to keep empty lines intact?
        self.keep_space = options['keep_space']
        # dict of license replacement options
        self.license_conversions = options['license_conversions']
        # dict of pkgconfig and other conversions
        self.pkgconfig_conversions = options['pkgconfig_conversions']
        self.perl_conversions = options['perl_conversions']
        self.cmake_conversions = options['cmake_conversions']
        self.tex_conversions = options['tex_conversions']
        # list of allowed groups
        self.allowed_groups = options['allowed_groups']
        # start the object
        self.paragraph = RpmPreambleElements(options)
        # initialize list of groups that need to pass over conversion fixer
        self.categories_with_package_tokens = self.paragraph.categories_with_sorted_package_tokens[:]
        # these packages actually need fixing after we sent the values to
        # reorder them
        self.categories_with_package_tokens.append('provides_obsoletes')
        # license handling
        self.subpkglicense = options['subpkglicense']

        # simple categories matching
        self.category_to_re = {
            'name': self.reg.re_name,
            'version': self.reg.re_version,
            # license need fix replacment
            'summary': self.reg.re_summary,
            'url': self.reg.re_url,
            'group': self.reg.re_group,
            'nosource': self.reg.re_nosource,
            # for source, we have a special match to keep the source number
            # for patch, we have a special match to keep the patch number
            'buildrequires': self.reg.re_buildrequires,
            'conflicts': self.reg.re_conflicts,
            # for prereq we append warning comment so we don't mess it there
            'requires': self.reg.re_requires,
            'recommends': self.reg.re_recommends,
            'suggests': self.reg.re_suggests,
            'enhances': self.reg.re_enhances,
            'supplements': self.reg.re_supplements,
            # for provides/obsoletes, we have a special case because we group them
            # for build root, we have a special match because we force its value
            'buildarch': self.reg.re_buildarch,
            'excludearch': self.reg.re_excludearch,
            'exclusivearch': self.reg.re_exclusivearch,
        }

        # deprecated definitions that we no longer want to see
        self.category_to_clean = {
            'vendor': self.reg.re_vendor,
            'autoreqprov': self.reg.re_autoreqprov,
            'epoch': self.reg.re_epoch,
            'icon': self.reg.re_icon,
            'copyright': self.reg.re_copyright,
            'packager': self.reg.re_packager,
            'debugpkg': self.reg.re_debugpkg,
            'prefix': self.reg.re_preamble_prefix,
        }

    def start_subparagraph(self):
        # Backup the list and start a new one
        self._oldstore.append(self.paragraph)
        self.paragraph = RpmPreambleElements(self.options)

    def _prune_ppc_condition(self):
        """
        Check if we have ppc64 obsolete and delete it
        """
        if not self.minimal and \
             isinstance(self.paragraph.items['conditions'][0], list) and \
             len(self.paragraph.items['conditions']) == 3 and \
             self.paragraph.items['conditions'][0][0] == '# bug437293' and \
             self.paragraph.items['conditions'][1].endswith('64bit'):
            self.paragraph.items['conditions'] = []

    def end_subparagraph(self, endif=False):
        if not self._oldstore:
            nested = False
        else:
            nested = True
        lines = self.paragraph.flatten_output(False, nested)
        if len(self.paragraph.items['define']) > 0 or \
           len(self.paragraph.items['bconds']) > 0:
            self._condition_define = True
        self.paragraph = self._oldstore.pop(-1)
        self.paragraph.items['conditions'] += lines

        # If we are on endif we check the condition content
        # and if we find the defines we put it on top.
        if endif or not self.condition:
            self._prune_ppc_condition()
            if self._condition_define:
                # If we have define conditions and possible bcond start
                # we need to put it bellow bcond definitions as otherwise
                # the switches do not have any effect
                if self._condition_bcond:
                    self.paragraph.items['bcond_conditions'] += self.paragraph.items['conditions']
                elif len(self.paragraph.items['define']) == 0:
                    self.paragraph.items['bconds'] += self.paragraph.items['conditions']
                else:
                    self.paragraph.items['define'] += self.paragraph.items['conditions']
                # in case the nested condition contains define we consider all parents
                # to require to be on top too;
                if len(self._oldstore) == 0:
                    self._condition_define = False
            else:
                self.paragraph.items['build_conditions'] += self.paragraph.items['conditions']

            # bcond must be reseted when on top and can be set even outside of the
            # define scope. So reset it here always
            if len(self._oldstore) == 0:
                self._condition_bcond = False
            self.paragraph.items['conditions'] = []

    def _split_name_and_version(self, value):
        # split the name and version from the requires element
        if self.reg.re_version_separator.match(value):
            match = self.reg.re_version_separator.match(value)
            pkgname = match.group(1)
            version = match.group(2)
        if not version:
            version = ''
        return pkgname, version

    def _fix_pkgconfig_name(self, value):
        # we just rename pkgconfig names to one unified one working everywhere
        pkgname, version = self._split_name_and_version(value)
        if pkgname == 'pkgconfig(pkg-config)' or \
           pkgname == 'pkg-config':
            # If we have pkgconfig dep in pkgconfig it is nuts, replace it
            return 'pkgconfig{0}'.format(version)
        else:
            return value

    def _pkgname_to_brackety(self, value, name, conversions):
        # we just want the pkgname if we have version string there
        # and for the pkgconfig deps we need to put the version into
        # the braces
        pkgname, version = self._split_name_and_version(value)
        converted = []
        if pkgname == 'pkgconfig':
            return [value]
        if pkgname not in conversions:
            # first check if the package is in the replacements
            return [value]
        else:
            # first split the data
            convers_list = conversions[pkgname].split()
            # then add each pkgconfig to the list
            # print pkgconf_list
            for j in convers_list:
                converted.append('{0}({1}){2}'.format(name, j, version))
        return converted

    def _fix_list_of_packages(self, value, category):
        # we do fix the package list only if there is no rpm call there on line
        # otherwise print there warning about nicer content and skip
        if self.reg.re_rpm_command.search(value):
            if not self.previous_line.startswith('#') and not self.minimal:
                self.paragraph.current_group.append('# FIXME: Use %requires_eq macro instead')
            return [value]
        tokens = DependencyParser(value).flat_out()
        # loop over all and do formatting as we can get more deps for one
        expanded = []
        for token in tokens:
            # there is allowed syntax => and =< ; hidious
            token = token.replace('=<', '<=')
            token = token.replace('=>', '>=')
            # we also skip all various rpm-macroed content as it
            # is usually not easy to determine how that should be
            # split
            if token.startswith('%'):
                expanded.append(token)
                continue
            # cleanup whitespace
            token = token.replace(' ', '')
            token = re.sub(r'([<>]=?|=)', r' \1 ', token)
            if not token:
                continue
            # replace pkgconfig name first
            token = self._fix_pkgconfig_name(token)
            # in scriptlets we most probably do not want the converted deps
            if category != 'prereq' and category != 'requires_phase':
                # here we go with descending priority to find match and replace
                # the strings by some optimistic value of brackety dep
                # priority is based on the first come first serve
                if self.pkgconfig:
                    token = self._pkgname_to_brackety(token, 'pkgconfig', self.pkgconfig_conversions)
                # checking if it is not list is simple avoidance of running
                # over already converted values
                if not isinstance(token, list) and self.perl:
                    token = self._pkgname_to_brackety(token, 'perl', self.perl_conversions)
                if not isinstance(token, list) and self.tex:
                    token = self._pkgname_to_brackety(token, 'tex', self.tex_conversions)
                if not isinstance(token, list) and self.cmake:
                    token = self._pkgname_to_brackety(token, 'cmake', self.cmake_conversions)
            if isinstance(token, str):
                expanded.append(token)
            else:
                expanded += token
        # and then sort them :)
        expanded.sort()
        return expanded

    def _add_line_value_to(self, category, value, key=None):
        """
        Change a key-value line, to make sure we have the right spacing.

        Note: since we don't have a key <-> category matching, we need to
        redo one. (Eg: Provides and Obsoletes are in the same category)
        """
        key = self.paragraph.compile_category_prefix(category, key)

        if category in self.categories_with_package_tokens:
            values = self._fix_list_of_packages(value, category)
        else:
            values = [value]

        for value in values:
            line = key + value
            self._add_line_to(category, line)

    def _add_line_to(self, category, line):
        if self.paragraph.current_group:
            self.paragraph.current_group.append(line)
            self.paragraph.items[category].append(self.paragraph.current_group)
            self.paragraph.current_group = []
        else:
            self.paragraph.items[category].append(line)

        self.previous_line = line

    def add(self, line):
        line = self._complete_cleanup(line)

        # if the line is empty, just skip it, unless keep_space is true
        if not self.keep_space and len(line) == 0:
            return

        # if it is multiline variable then we need to append to previous content
        # also multiline is allowed only for define lines so just cheat and
        # know ahead
        elif self.multiline:
            self._add_line_to('define', line)
            # if it is no longer trailed with backslash stop
            if not line.endswith('\\'):
                self.multiline = False
            return

        # If we match the if else or endif we create subgroup
        # this is basically our class again until we match
        # else where we mark end of paragraph or endif
        # which mark the end of our subclass and that we can
        # return the data to our main class for at-bottom placement
        elif self.reg.re_if.match(line) or self.reg.re_codeblock.match(line):
            self._add_line_to('conditions', line)
            self.condition = True
            # check for possibility of the bcond conditional
            if "%{with" in line or "%{without" in line:
                self._condition_bcond = True
            self.start_subparagraph()
            self.previous_line = line
            return

        elif self.reg.re_else.match(line):
            if self.condition:
                self._add_line_to('conditions', line)
                self.end_subparagraph()
                self.start_subparagraph()
            self.previous_line = line
            return

        elif self.reg.re_endif.match(line) or self.reg.re_endcodeblock.match(line):
            self._add_line_to('conditions', line)
            # Set conditions to false only if we are
            # closing last of the nested ones
            if len(self._oldstore) == 1:
                self.condition = False
            self.end_subparagraph(True)
            self.previous_line = line
            return

        elif self.reg.re_comment.match(line):
            if line or self.previous_line:
                self.paragraph.current_group.append(line)
                self.previous_line = line
            return

        elif self.reg.re_source.match(line):
            match = self.reg.re_source.match(line)
            self._add_line_value_to('source', match.group(2), key='Source%s' % match.group(1))
            return

        elif self.reg.re_patch.match(line):
            match = self.reg.re_patch.match(line)
            # convert Patch: to Patch0:
            if match.group(2) == '':
                zero = '0'
            else:
                zero = ''
            self._add_line_value_to('patch', match.group(3), key='%sPatch%s%s' % (match.group(1), zero, match.group(2)))
            return

        elif self.reg.re_bcond_with.match(line):
            self._add_line_to('bconds', line)
            return

        elif self.reg.re_define.match(line) or self.reg.re_global.match(line) or self.reg.re_onelinecond.match(line):
            if line.endswith('\\'):
                self.multiline = True
            # if we are kernel and not multiline we need to be at bottom, so
            # lets use misc section, otherwise go for define
            if not self.multiline and line.find("kernel_module") >= 0:
                self._add_line_to('misc', line)
            else:
                self._add_line_to('define', line)
            return

        elif self.reg.re_requires_eq.match(line):
            match = self.reg.re_requires_eq.match(line)
            self._add_line_value_to('requires_eq', match.group(1))
            return

        elif self.reg.re_prereq.match(line):
            match = self.reg.re_prereq.match(line)
            self._add_line_value_to('prereq', match.group(1))
            return

        elif self.reg.re_requires_phase.match(line):
            match = self.reg.re_requires_phase.match(line)
            # Put the requires content properly as key for formatting
            self._add_line_value_to('requires_phase', match.group(2), key='Requires{0}'.format(match.group(1)))
            return

        elif self.reg.re_provides.match(line):
            match = self.reg.re_provides.match(line)
            self._add_line_value_to('provides_obsoletes', match.group(1), key='Provides')
            return

        elif self.reg.re_obsoletes.match(line):
            match = self.reg.re_obsoletes.match(line)
            self._add_line_value_to('provides_obsoletes', match.group(1), key='Obsoletes')
            return

        elif self.reg.re_buildroot.match(line):
            # we only are fine with buildroot only once
            if len(self.paragraph.items['buildroot']) == 0:
                self._add_line_value_to('buildroot', '%{_tmppath}/%{name}-%{version}-build')
            return

        elif self.reg.re_license.match(line):
            # first convert the license string to proper format and then append
            match = self.reg.re_license.match(line)
            value = match.groups()[len(match.groups()) - 1]
            value = fix_license(value, self.license_conversions)
            # only store subpkgs if they have different licenses
            if not (type(self).__name__ == 'RpmPackage' and not self.subpkglicense):
                self._add_line_value_to('license', value)
            return

        elif self.reg.re_release.match(line):
            match = self.reg.re_release.match(line)
            value = match.group(1)
            if re.search(r'[a-zA-Z\s]', value):
                self._add_line_value_to('release', value)
            else:
                self._add_line_value_to('release', '0')
            return

        elif self.reg.re_summary_localized.match(line):
            match = self.reg.re_summary_localized.match(line)
            # we need to know what language we need
            language = match.group(1)
            # and what value is there
            content = match.group(2)
            self._add_line_value_to('summary_localized', content, key='Summary{0}'.format(language))
            return

        elif self.reg.re_group.match(line):
            match = self.reg.re_group.match(line)
            value = match.group(1)
            if not self.minimal:
                if self.previous_line and not self.previous_line.startswith('# FIXME') and value not in self.allowed_groups:
                    self.paragraph.current_group.append('# FIXME: use correct group, see "https://en.opensuse.org/openSUSE:Package_group_guidelines"')
            self._add_line_value_to('group', value)
            return

        # loop for all other matching categories which
        # do not require special attention
        else:
            # cleanup
            for (category, regexp) in self.category_to_clean.items():
                match = regexp.match(line)
                if match:
                    return

            # simple matching
            for (category, regexp) in self.category_to_re.items():
                match = regexp.match(line)
                if match:
                    # instead of matching first group as there is only one,
                    # take the last group
                    # (so I can have more advanced regexp for RPM tags)
                    self._add_line_value_to(category, match.groups()[len(match.groups()) - 1])
                    return

            self._add_line_to('misc', line)

    def output(self, fout, newline=True, new_class=None):
        lines = self.paragraph.flatten_output(self.subpkglicense)
        self.lines += lines
        Section.output(self, fout, newline, new_class)
