#!/usr/bin/env python3
"""Benchmark model and helper functions."""
import subprocess
from functools import reduce
import operator
import sys
from distutils.spawn import find_executable
import re
from collections import defaultdict
import string
from pprint import pprint

from kerncraft.prefixedunit import PrefixedUnit


def group_iterator(group):
    """
    Iterate over simple regex-like groups.

    The only special character is a dash (-), which take the preceding and the following chars to
    compute a range. If the range is non-sensical (e.g., b-a) it will be empty

    Example:
    >>> list(group_iterator('a-f'))
    ['a', 'b', 'c', 'd', 'e', 'f']
    >>> list(group_iterator('148'))
    ['1', '4', '8']
    >>> list(group_iterator('7-9ab'))
    ['7', '8', '9', 'a', 'b']
    >>> list(group_iterator('0B-A1'))
    ['0', '1']
    """
    ordered_chars = string.ascii_letters + string.digits
    tokenizer = ('(?P<seq>[a-zA-Z0-9]-[a-zA-Z0-9])|'
                 '(?P<chr>.)')
    for m in re.finditer(tokenizer, group):
        if m.group('seq'):
            start, sep, end = m.group('seq')
            for i in range(ordered_chars.index(start), ordered_chars.index(end) + 1):
                yield ordered_chars[i]
        else:
            yield m.group('chr')


def register_options(regdescr):
    """
    Very reduced regular expressions for describing a group of registers.

    Only groups in square bracktes and unions with pipes (|) are supported.

    Examples:
    >>> list(register_options('PMC[0-3]'))
    ['PMC0', 'PMC1', 'PMC2', 'PMC3']
    >>> list(register_options('MBOX0C[01]'))
    ['MBOX0C0', 'MBOX0C1']
    >>> list(register_options('CBOX2C1'))
    ['CBOX2C1']
    >>> list(register_options('CBOX[0-3]C[01]'))
    ['CBOX0C0', 'CBOX0C1', 'CBOX1C0', 'CBOX1C1', 'CBOX2C0', 'CBOX2C1', 'CBOX3C0', 'CBOX3C1']
    >>> list(register_options('PMC[0-1]|PMC[23]'))
    ['PMC0', 'PMC1', 'PMC2', 'PMC3']
    """
    if not regdescr:
        yield None
    tokenizer = ('\[(?P<grp>[^]]+)\]|'
                 '(?P<chr>.)')
    for u in regdescr.split('|'):
        m = re.match(tokenizer, u)

        if m.group('grp'):
            current = group_iterator(m.group('grp'))
        else:
            current = [m.group('chr')]

        for c in current:
            if u[m.end():]:
                for r in register_options(u[m.end():]):
                    yield c + r
            else:
                yield c


def eventstr(event_tuple=None, event=None, register=None, parameters=None):
    """
    Return a LIKWID event string from an event tuple or keyword arguments.

    *event_tuple* may have two or three arguments: (event, register) or
    (event, register, parameters)

    Keyword arguments will be overwritten by *event_tuple*.

    >>> eventstr(('L1D_RE'+'PLACEMENT', 'PMC0', None))
    'L1D_RE'+'PLACEMENT:PMC0'
    >>> eventstr(('L1D_RE'+'PLACEMENT', 'PMC0'))
    'L1D_RE'+'PLACEMENT:PMC0'
    >>> eventstr(('MEM_UOPS_RETIRED_LOADS', 'PMC3', {'EDGEDETECT': None, 'THRESHOLD': 2342}))
    'MEM_UOPS_RETIRED_LOADS:PMC3:EDGEDETECT:THRESHOLD=0x926'
    >>> eventstr(event='DTLB_LOAD_MISSES_WALK_DURATION', register='PMC3')
    'DTLB_LOAD_MISSES_WALK_DURATION:PMC3'
    """
    if len(event_tuple) == 3:
        event, register, parameters = event_tuple
    elif len(event_tuple) == 2:
        event, register = event_tuple
    event_dscr = [event, register]

    if parameters:
        for k, v in sorted(event_tuple[2].items()):  # sorted for reproducability
            if type(v) is int:
                k += "={}".format(hex(v))
            event_dscr.append(k)
    return ":".join(event_dscr)


def build_minimal_runs(events):
    """Compile list of minimal runs for given events."""
    # Eliminate multiples
    events = [e for i, e in enumerate(events) if events.index(e) == i]

    # Build list of runs per register group
    scheduled_runs = {}
    scheduled_events = []
    cur_run = 0
    while len(scheduled_events) != len(events):
        for event_tpl in events:
            event, registers, parameters = event_tpl
            # Skip allready scheduled events
            if event_tpl in scheduled_events:
                continue
            # Compile explicit list of possible register locations
            for possible_reg in register_options(registers):
                # Schedule in current run, if register is not yet in use
                s = scheduled_runs.setdefault(cur_run, {})
                if possible_reg not in s:
                    s[possible_reg] = (event, possible_reg, parameters)
                    # ban from further scheduling attempts
                    scheduled_events.append(event_tpl)
                    break
        cur_run += 1

    # Collaps all register dicts to single runs
    runs = [list(v.values()) for v in scheduled_runs.values()]

    return runs


class Benchmark(object):
    """Produce a benchmarkable binary to be used with likwid."""

    name = "benchmark"

    @classmethod
    def configure_arggroup(cls, parser):
        """Configure argument parser."""
        parser.add_argument(
            '--no-phenoecm', action='store_true',
            help='Disables the phenomenological ECM model building.')

    def __init__(self, kernel, machine, args=None, parser=None, no_phenoecm=False, verbose=0):
        """
        Create Benchmark model from kernel and machine objects.

        *kernel* is a Kernel object
        *machine* describes the machine (cpu, cache and memory) characteristics
        *args* (optional) are the parsed arguments from the comand line

        If *args* is None, *no_phenoecm* and *verbose* are used.
        """
        self.kernel = kernel
        self.machine = machine
        self._args = args
        self._parser = parser
        self.results = None

        if args:
            self.no_phenoecm = args.no_phenoecm
            self.verbose = args.verbose
        else:
            self.no_phenoecm = no_phenoecm
            self.verbose = verbose

        cpuinfo = ''
        try:
            with open('/proc/cpuinfo') as f:
                current_cpu_model = re.search(r'^model name\s+:\s+(.+?)\s*$',
                                              f.read(),
                                              flags=re.MULTILINE).groups()[0]
        except (AttributeError, FileNotFoundError):
            current_cpu_model = None
        if self.machine['model name'] != current_cpu_model:
            print("WARNING: current CPU model and machine description do not "
                  "match. ({!r} vs {!r})".format(self.machine['model name'],
                                                 current_cpu_model))
        try:
            current_cpu_freq = re.search(r'^cpu MHz\s+:\s+'
                                         r'([0-9]+(?:\.[0-9]+)?)\s*$',
                                         cpuinfo,
                                         flags=re.MULTILINE).groups()[0]
            current_cpu_freq = float(current_cpu_freq) * 1e6
        except AttributeError:
            current_cpu_freq = None
        if float(self.machine['clock']) != current_cpu_freq:
            print("WARNING: current CPU frequency and machine description do "
                  "not match. ({!r} vs {!r})".format(float(self.machine['clock']),
                                                     current_cpu_freq))

    def perfctr(self, cmd, group='MEM', cpu='S0:0', code_markers=True, pin=True):
        """
        Run *cmd* with likwid-perfctr and returns result as dict.

        *group* may be a performance group known to likwid-perfctr or an event string.
        Only works with single core!
        """
        # Making sure likwid-perfctr is available:
        if find_executable('likwid-perfctr') is None:
            print("likwid-perfctr was not found. Make sure likwid is installed and found in PATH.",
                  file=sys.stderr)
            sys.exit(1)

        # FIXME currently only single core measurements support!
        perf_cmd = ['likwid-perfctr', '-f', '-O', '-g', group]

        if pin:
            perf_cmd += ['-C', cpu]
        else:
            perf_cmd += ['-c', cpu]

        if code_markers:
            perf_cmd.append('-m')

        perf_cmd += cmd
        if self.verbose > 1:
            print(' '.join(perf_cmd))
        try:
            output = subprocess.check_output(perf_cmd).decode('utf-8').split('\n')
        except subprocess.CalledProcessError as e:
            print("Executing benchmark failed: {!s}".format(e), file=sys.stderr)
            sys.exit(1)

        results = {}
        for line in output:
            line = line.split(',')
            try:
                # Metrics
                results[line[0]] = float(line[1])
            except ValueError:
                # Would not convert to float
                pass
            except IndexError:
                # Not a parable line (did not contain any commas)
                continue
            try:
                # Event counters
                counter_value = int(line[2])
                if re.fullmatch(r'[A-Z0-9_]+', line[0]) and re.fullmatch(r'[A-Z0-9]+', line[1]):
                    results.setdefault(line[0], {})
                    results[line[0]][line[1]] = counter_value
            except (IndexError, ValueError):
                pass

        return results

    def analyze(self):
        """Run analysis."""
        bench = self.kernel.build(verbose=self.verbose > 1)
        element_size = self.kernel.datatypes_size[self.kernel.datatype]

        # Build arguments to pass to command:
        args = [bench] + [str(s) for s in list(self.kernel.constants.values())]

        # Determine base runtime with 100 iterations
        runtime = 0.0
        time_per_repetition = 0.2 / 10.0
        repetitions = 10
        mem_results = {}

        while runtime < 0.15:
            # Interpolate to a 0.2s run
            if time_per_repetition == 0.0:
                repetitions = 0.2 // time_per_repetition
            else:
                repetitions *= 10

            mem_results = self.perfctr(args + [str(repetitions)], group="MEM")
            runtime = mem_results['Runtime (RDTSC) [s]']
            time_per_repetition = runtime / float(repetitions)
        raw_results = [mem_results]

        # Gather remaining counters
        if not self.no_phenoecm:
            # Build events and sympy expressions for all model metrics
            T_OL, event_counters = self.machine.parse_perfmetric(
                self.machine['overlapping model']['performance counter metric'])
            T_data, event_dict = self.machine.parse_perfmetric(
                self.machine['non-overlapping model']['performance counter metric'])
            event_counters.update(event_dict)
            cache_metrics = defaultdict(dict)
            for i in range(len(self.machine['memory hierarchy']) - 1):
                cache_info = self.machine['memory hierarchy'][i]
                name = cache_info['level']
                for k, v in cache_info['performance counter metrics'].items():
                    cache_metrics[name][k], event_dict = self.machine.parse_perfmetric(v)
                    event_counters.update(event_dict)

            # Compile minimal runs to gather all required events
            minimal_runs = build_minimal_runs(list(event_counters.values()))
            measured_ctrs = {}
            for run in minimal_runs:
                ctrs = ','.join([eventstr(e) for e in run])
                r = self.perfctr(args + [str(repetitions)], group=ctrs)
                raw_results.append(r)
                measured_ctrs.update(r)
            # Match measured counters to symbols
            event_counter_results = {}
            for sym, ctr in event_counters.items():
                event, regs, parameter = ctr[0], register_options(ctr[1]), ctr[2]
                for r in regs:
                    if r in measured_ctrs[event]:
                        event_counter_results[sym] = measured_ctrs[event][r]

            # Analytical metrics needed for futher calculation
            elements_per_cacheline = float(self.machine['cacheline size']) // element_size
            total_iterations = self.kernel.iteration_length() * repetitions
            total_cachelines = total_iterations / elements_per_cacheline

            T_OL_result = T_OL.subs(event_counter_results) / total_cachelines
            cache_metric_results = defaultdict(dict)
            for cache, mtrcs in cache_metrics.items():
                for m, e in mtrcs.items():
                    cache_metric_results[cache][m] = e.subs(event_counter_results)

            # Inter-cache transfers per CL
            cache_transfers_per_cl = {cache: {k: PrefixedUnit(v / total_cachelines, 'CL/CL')
                                              for k, v in d.items()}
                                      for cache, d in cache_metric_results.items()}
            cache_transfers_per_cl['L1']['accesses'].unit = 'LOAD/CL'

            # Select appropriate bandwidth
            mem_bw, mem_bw_kernel = self.machine.get_bandwidth(
                -1,  # mem
                cache_metric_results['L3']['misses'],  # load_streams
                cache_metric_results['L3']['evicts'],  # store_streams
                1)

            data_transfers = {
                # Assuming 0.5 cy / LOAD (SSE on SNB or IVB; AVX on HSW, BDW, SKL or SKX)
                'T_nOL': (cache_metric_results['L1']['accesses'] / total_cachelines * 0.5),
                'T_L1L2': ((cache_metric_results['L1']['misses'] +
                            cache_metric_results['L1']['evicts']) /
                           total_cachelines *
                           self.machine['memory hierarchy'][0]['cycles per cacheline transfer']),
                'T_L2L3': ((cache_metric_results['L2']['misses'] +
                            cache_metric_results['L2']['evicts']) /
                           total_cachelines *
                           self.machine['memory hierarchy'][1]['cycles per cacheline transfer']),
                'T_L3MEM': ((cache_metric_results['L3']['misses'] +
                             cache_metric_results['L3']['evicts']) *
                            float(self.machine['cacheline size']) /
                            total_cachelines / mem_bw *
                            float(self.machine['clock']))
            }

            # Build phenomenological ECM model:
            ecm_model = {'T_OL': T_OL_result}
            ecm_model.update(data_transfers)
        else:
            event_counters = {}
            ecm_model = None
            cache_transfers_per_cl = None

        self.results = {'raw output': raw_results, 'ECM': ecm_model,
                        'data transfers': cache_transfers_per_cl,
                        'Runtime (per repetition) [s]': time_per_repetition,
                        'event counters': event_counters}

        # TODO make more generic to support other (and multiple) constant names
        iterations_per_repetition = reduce(
            operator.mul,
            [self.kernel.subs_consts(max_ - min_) / self.kernel.subs_consts(step)
             for idx, min_, max_, step in self.kernel._loop_stack],
            1)
        self.results['Iterations per repetition'] = iterations_per_repetition
        iterations_per_cacheline = float(self.machine['cacheline size']) / element_size
        cys_per_repetition = time_per_repetition * float(self.machine['clock'])
        self.results['Runtime (per cacheline update) [cy/CL]'] = \
            (cys_per_repetition / iterations_per_repetition) * iterations_per_cacheline
        self.results['MEM volume (per repetition) [B]'] = \
            mem_results['Memory data volume [GBytes]'] * 1e9 / repetitions
        self.results['Performance [MFLOP/s]'] = \
            sum(self.kernel._flops.values()) / (
            time_per_repetition / iterations_per_repetition) / 1e6
        if 'Memory bandwidth [MBytes/s]' in mem_results:
            self.results['MEM BW [MByte/s]'] = mem_results['Memory bandwidth [MBytes/s]']
        else:
            self.results['MEM BW [MByte/s]'] = mem_results['Memory BW [MBytes/s]']
        self.results['Performance [MLUP/s]'] = (
                                               iterations_per_repetition / time_per_repetition) / 1e6
        self.results['Performance [MIt/s]'] = (
                                              iterations_per_repetition / time_per_repetition) / 1e6

    def report(self, output_file=sys.stdout):
        """Report gathered analysis data in human readable form."""
        if self.verbose > 1:
            pprint(self.results)

        if self.verbose > 0:
            print('Runtime (per repetition): {:.2g} s'.format(
                self.results['Runtime (per repetition) [s]']),
                file=output_file)
        if self.verbose > 0:
            print('Iterations per repetition: {!s}'.format(
                self.results['Iterations per repetition']),
                file=output_file)
        print('Runtime (per cacheline update): {:.2f} cy/CL'.format(
            self.results['Runtime (per cacheline update) [cy/CL]']),
            file=output_file)
        print('MEM volume (per repetition): {:.0f} Byte'.format(
            self.results['MEM volume (per repetition) [B]']),
            file=output_file)
        print('Performance: {:.2f} MFLOP/s'.format(self.results['Performance [MFLOP/s]']),
              file=output_file)
        print('Performance: {:.2f} MLUP/s'.format(self.results['Performance [MLUP/s]']),
              file=output_file)
        print('Performance: {:.2f} It/s'.format(self.results['Performance [MIt/s]']),
              file=output_file)
        if self.verbose > 0:
            print('MEM bandwidth: {:.2f} MByte/s'.format(self.results['MEM BW [MByte/s]']),
                  file=output_file)
        print('', file=output_file)

        if not self.no_phenoecm:
            print("Data Transfers:")
            print("{:^8} |".format("cache"), end='')
            for metrics in self.results['data transfers'].values():
                for metric_name in metrics:
                    print(" {:^14}".format(metric_name), end='')
                print()
                break
            for cache, metrics in self.results['data transfers'].items():
                print("{!s:^8} |".format(cache), end='')
                for v in metrics.values():
                    print(" {!s:^14}".format(v), end='')
                print()
            print()

            print('Phenomenological ECM model: {{ {T_OL:.1f} || {T_nOL:.1f} | {T_L1L2:.1f} | '
                  '{T_L2L3:.1f} | {T_L3MEM:.1f} }} cy/CL'.format(
                **{k: float(v) for k, v in self.results['ECM'].items()}),
                file=output_file)
            print('T_OL assumes that two loads per cycle may be retiered, which is true for '
                  '128bit SSE/half-AVX loads on SNB and IVY, and 256bit full-AVX loads on HSW, '
                  'BDW, SKL and SKX, but it also depends on AGU availability.',
                  file=output_file)
