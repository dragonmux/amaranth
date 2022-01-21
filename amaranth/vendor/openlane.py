from abc import abstractproperty

import textwrap
import re
import jinja2
import os
from markupsafe import Markup

from .. import __version__
from .._toolchain import *
from ..hdl import *
from ..hdl.ir import Fragment
from ..hdl.xfrm import SampleLowerer, DomainLowerer
from ..lib.cdc import ResetSynchronizer
from ..back import rtlil, verilog
from ..build.res import *
from ..build.run import *
from ..lib.cdc import ResetSynchronizer
from ..build import *

__all__ = ["OpenLANEPlatform", "Sky130FDSCHDPlatform", "Sky130FDSCHSPlatform", "Sky130FDSCMSPlatform", "Sky130FDSCLSPlatform", "Sky130FDSCHDLLPlatform"]

class OpenLANEPlatform(TemplatedPlatform):
    """
    OpenLANE ASIC Flow
    ------------------

    **NOTE:** See https://github.com/The-OpenROAD-Project/OpenLane#setting-up-openlane for
    information on how to setup OpenLANE and the sky130 PDK.

    For information the the possible `flow_settings` that can be set, see
    https://openlane.readthedocs.io/en/latest/configuration/README.html#variables-information

    Required tools:
        * ``openlane``
        * ``docker``

    Build products:
        * ``config.tcl``: OpenLANE configuration script.
        * ``{{name}}.sdc``: Clock constraints.
        * ``{{name}}.v``: Design verilog
        * ``{{name}}.debug.v``: Design debug verilog
        * ``runs/*``: OpenLANE flow output

    """

    toolchain = None
    openlane_root = ''
    pdk_path = ''

    cell_library = abstractproperty()
    flow_settings = abstractproperty()

    # known default
    _build_dir = 'nya'

    # Common Templates

    _common_file_templates = {
        **TemplatedPlatform.build_script_templates,
        """build_{{name}}.sh""": r"""
            # {{autogenerated}}
            set -e{{verbose("x")}}
            if [ -z "$BASH" ] ; then exec /bin/bash "$0" "$@"; fi
            {{emit_commands("sh")}}
        """,
        """config.tcl""": r"""
            # {{autogenerated}}
            # Design Information
            set ::env(DESIGN_NAME) "{{name}}"
            set ::env(VERILOG_FILES) "/design_{{name}}/{{name}}.v"
            set ::env(SDC_FILE) "/design_{{name}}/{{name}}.sdc"
            {% if platform.default_clk %}
            # Clock Settings
            set ::env(CLOCK_PERIOD) "{{platform.default_clk_constraint.period / 1e-9}}"
            set ::env(CLOCK_PORT) "{{platform._default_clk_name}}"
            set ::env(CLOCK_NET) $::env(CLOCK_PORT)
            {% else %}
            # Disable the clock
            set ::env(CLOCK_TREE_SYNTH) 0
            set ::env(CLOCK_PORT) ""
            {% endif %}
            # PDK Settings
            set ::env(PDK) "{{platform.pdk}}"
            set ::env(STD_CELL_LIBRARY) "{{platform.cell_library}}"

            {% for s, v in platform.flow_settings.items() %}
            set ::env({{s}}) {{v|tcl_escape}}
            {% endfor %}

            # Pull in PDK specific settings
            set filename $::env(DESIGN_DIR)/$::env(PDK)_$::env(STD_CELL_LIBRARY)_config.tcl
            if { [file exists $filename] == 1} {
                source $filename
            }
        """,
        "{{name}}.v": r"""
            /* {{autogenerated}} */
            {{emit_verilog()}}
        """,
        "{{name}}.debug.v": r"""
            /* {{autogenerated}} */
            {{emit_debug_verilog()}}
        """,
        "{{name}}.sdc": r"""
            # {{autogenerated}}
            {% for net_signal, port_signal, frequency in platform.iter_clock_constraints() -%}
                {% if port_signal is not none -%}
                    create_clock -name {{port_signal.name|tcl_escape}} -period {{1000000000/frequency}} [get_ports {{port_signal.name|tcl_escape}}]
                {% else -%}
                    create_clock -name {{net_signal.name|tcl_escape}} -period {{1000000000/frequency}} [get_nets {{net_signal|hierarchy("/")|tcl_escape}}]
                {% endif %}
            {% endfor %}
            {{get_override("add_constraints")|default("# (add_constraints placeholder)")}}
        """,
    }

    # Container Templates

    _container_required_tools = ["docker"]

    _container_file_templates = {
        **_common_file_templates,
    }

    _container_command_templates = [
        r"""
        {{invoke_tool("docker")}}
            run
            -it
            --rm
            -v {{get_override("OpenLANE")|default(platform.openlane_root)}}:/openLANE_flow
            -v {{get_override("PDKPath")|default(platform.pdk_path)}}:/PDK
            -v /tmp/{{platform._build_dir}}:/design_{{name}}
            -e PDK_ROOT=/PDK
            -u {{getuid()}}:{{getgid()}}
            efabless/openlane:{{get_override("openlane_version")|default("latest")}}
            sh -c "./flow.tcl -design /design_{{name}}"
        """
    ]

    # Local Templates

    _local_required_tools = [

    ]

    _local_file_templates = {
        **_common_file_templates,

    }

    _local_command_templates = [
        r"""s
            sh -c {{get_override("OpenLANE")|default(platform.openlane_root)}}/flow.tcl -design {{name}}
        """
    ]

    def __init__(self, *, toolchain = "Docker"):
        super().__init__()

        assert toolchain in ("Docker", "Local")
        self.toolchain = toolchain

    @property
    def required_tools(self):
        if self.toolchain == "Docker":
            return self._container_required_tools
        if self.toolchain == "Local":
            return self._local_required_tools
        assert False

    @property
    def file_templates(self):
        if self.toolchain == "Docker":
            return self._container_file_templates
        if self.toolchain == "Local":
            return self._local_file_templates
        assert False

    @property
    def command_templates(self):
        if self.toolchain == "Docker":
            return self._container_command_templates
        if self.toolchain == "Local":
            return self._local_command_templates
        assert False

    @property
    def _default_clk_name(self):
        if self.default_clk is None:
            raise AttributeError("Platform '{}' does not define a default clock"
                                 .format(type(self).__name__))
        resource = self.lookup(self.default_clk)
        #port = self._requested[resource.name, resource.number]
        #assert isinstance(resource.ios[0], Pins)
        for res, pin, port, attrs in self._ports:
            if res == resource:
                if res.ios[0].dir == 'i':
                    return pin.i.name
                else:
                    return pin.o.name
        raise AssertionError(f"Platform '{type(self).__name__}' defined default clock but no matching resource")


    # This is a silly hack but we need to know the build dir
    def build(self, *args, **kwargs):
        self._build_dir = kwargs.get('build_dir', 'build')

        super().build(*args, **kwargs)


    # This was lifted directly from the TemplatedPlatform.toolchain_prepare because I needed to tweak it a bit
    def prepare(self, elaboratable, name, **kwargs):
        # Restrict the name of the design to a strict alphanumeric character set. Platforms will
        # interpolate the name of the design in many different contexts: filesystem paths, Python
        # scripts, Tcl scripts, ad-hoc constraint files, and so on. It is not practical to add
        # escaping code that handles every one of their edge cases, so make sure we never hit them
        # in the first place.
        invalid_char = re.match(r"[^A-Za-z0-9_]", name)
        if invalid_char:
            raise ValueError("Design name {!r} contains invalid character {!r}; only alphanumeric "
                             "characters are valid in design names"
                             .format(name, invalid_char.group(0)))

        # This notice serves a dual purpose: to explain that the file is autogenerated,
        # and to incorporate the nMigen version into generated code.
        autogenerated = "Automatically generated by nMigen {}. Do not edit.".format(__version__)

        assert 'ports' in kwargs
        ports = kwargs['ports']

        fragment = Fragment.get(elaboratable, self)
        for resource, pin, port, attrs in self._ports:
            for signal in pin.fields:
                ports.append(pin[signal])

        fragment = fragment.prepare(missing_domain=self.create_missing_domain(ports), **kwargs)
        rtlil_text, self._name_map = rtlil.convert_fragment(fragment, name)

        def emit_rtlil():
            return rtlil_text

        def emit_verilog(opts=()):
            return verilog._convert_rtlil_text(rtlil_text,
                strip_internal_attrs=True, write_verilog_opts=opts)

        def emit_debug_verilog(opts=()):
            return verilog._convert_rtlil_text(rtlil_text,
                strip_internal_attrs=False, write_verilog_opts=opts)

        def emit_commands(syntax):
            commands = []

            for name in self.required_tools:
                env_var = tool_env_var(name)
                if syntax == "sh":
                    template = ": ${{{env_var}:={name}}}"
                elif syntax == "bat":
                    template = \
                        "if [%{env_var}%] equ [\"\"] set {env_var}=\n" \
                        "if [%{env_var}%] equ [] set {env_var}={name}"
                else:
                    assert False
                commands.append(template.format(env_var=env_var, name=name))

            for index, command_tpl in enumerate(self.command_templates):
                command = render(command_tpl, origin="<command#{}>".format(index + 1),
                                 syntax=syntax)
                command = re.sub(r"\s+", " ", command)
                if syntax == "sh":
                    commands.append(command)
                elif syntax == "bat":
                    commands.append(command + " || exit /b")
                else:
                    assert False

            return "\n".join(commands)

        def get_override(var):
            var_env = "AMARANTH_ENV_{}".format(var)
            if var_env in os.environ:
                # On Windows, there is no way to define an "empty but set" variable; it is tempting
                # to use a quoted empty string, but it doesn't do what one would expect. Recognize
                # this as a useful pattern anyway, and treat `set VAR=""` on Windows the same way
                # `export VAR=` is treated on Linux.
                return re.sub(r'^\"\"$', "", os.environ[var_env])
            elif var in kwargs:
                if isinstance(kwargs[var], str):
                    return textwrap.dedent(kwargs[var]).strip()
                else:
                    return kwargs[var]
            else:
                return jinja2.Undefined(name=var)

        @jinja2.contextfunction
        def invoke_tool(context, name):
            env_var = tool_env_var(name)
            if context.parent["syntax"] == "sh":
                return "\"${}\"".format(env_var)
            elif context.parent["syntax"] == "bat":
                return "%{}%".format(env_var)
            else:
                assert False

        def options(opts):
            if isinstance(opts, str):
                return opts
            else:
                return " ".join(opts)

        def hierarchy(signal, separator):
            return separator.join(self._name_map[signal][1:])

        def ascii_escape(string):
            def escape_one(match):
                if match.group(1) is None:
                    return match.group(2)
                else:
                    return "_{:02x}_".format(ord(match.group(1)[0]))
            return "".join(escape_one(m) for m in re.finditer(r"([^A-Za-z0-9_])|(.)", string))

        def tcl_escape(string):
            if isinstance(string, Markup):
                return string
            else:
                string = str(string)
            return "{" + re.sub(r"([{}\\])", r"\\\1", string) + "}"

        def tcl_quote(string):
            if isinstance(string, Markup):
                return string
            else:
                string = str(string)
            return '"' + re.sub(r"([$[\\])", r"\\\1", string) + '"'

        def verbose(arg):
            if get_override("verbose"):
                return arg
            else:
                return jinja2.Undefined(name="quiet")

        def quiet(arg):
            if get_override("verbose"):
                return jinja2.Undefined(name="quiet")
            else:
                return arg

        def render(source, origin, syntax=None):
            try:
                source   = textwrap.dedent(source).strip()
                compiled = jinja2.Template(source,
                    trim_blocks=True, lstrip_blocks=True, undefined=jinja2.StrictUndefined)
                compiled.environment.filters["options"] = options
                compiled.environment.filters["hierarchy"] = hierarchy
                compiled.environment.filters["ascii_escape"] = ascii_escape
                compiled.environment.filters["tcl_escape"] = tcl_escape
                compiled.environment.filters["tcl_quote"] = tcl_quote
            except jinja2.TemplateSyntaxError as e:
                e.args = ("{} (at {}:{})".format(e.message, origin, e.lineno),)
                raise
            return compiled.render({
                "name": name,
                "platform": self,
                "emit_rtlil": emit_rtlil,
                "emit_verilog": emit_verilog,
                "emit_debug_verilog": emit_debug_verilog,
                "emit_commands": emit_commands,
                "syntax": syntax,
                "invoke_tool": invoke_tool,
                "get_override": get_override,
                "verbose": verbose,
                "quiet": quiet,
                "autogenerated": autogenerated,
                "getuid": os.getuid,
                "getgid": os.getgid,
            })

        plan = BuildPlan(script="build_{}".format(name))
        for filename_tpl, content_tpl in self.file_templates.items():
            plan.add_file(render(filename_tpl, origin=filename_tpl),
                          render(content_tpl, origin=content_tpl))
        for filename, content in self.extra_files.items():
            plan.add_file(filename, content)
        return plan

    def create_missing_domain(self, ports):
        def create_domain(name):
            if name == "sync" and self.default_clk is not None:
                clk_i = self.request(self.default_clk).i
                ports.append(clk_i)
                m = Module()

                if self.default_rst is not None:
                    rst_i = self.request(self.default_rst).i
                else:
                    assert 'rst' not in (signal.name for signal in ports)
                    rst_i = Signal(name = 'rst')
                    ports.append(rst_i)
                    self.default_rst = 'rst'

                m.domains += ClockDomain("sync")
                m.d.comb += ClockSignal("sync").eq(clk_i)
                m.submodules.reset_sync = ResetSynchronizer(rst_i, domain="sync")

                return m
        return create_domain


# PDK Key:
# FD - Foundry
# SC - Standard Cell
# HD - High Density
# HDLL - High Density Low Leakage
# LS/MS/HS Low/Medium/High Speeds

class Sky130HighDensityPlatform(OpenLANEPlatform):
    """
    sky130A sky130_fd_sc_hd OpenLANE Flow
    ------------------

    **NOTE:** See https://github.com/The-OpenROAD-Project/OpenLane#setting-up-openlane for
    information on how to setup OpenLANE and the sky130 PDK.

    For information the the possible `flow_settings` that can be set, see
    https://openlane.readthedocs.io/en/latest/configuration/README.html#variables-information

    Required tools:
        * ``openlane``
        * ``docker``

    Build products:
        * ``config.tcl``: OpenLANE configuration script.
        * ``{{name}}.sdc``: Clock constraints.
        * ``{{name}}.v``: Design verilog
        * ``{{name}}.debug.v``: Design debug verilog
        * ``runs/*``: OpenLANE flow output

    """
    pdk = "sky130A"
    cell_library = "sky130_fd_sc_hd"

class Sky130HighSpeedPlatform(OpenLANEPlatform):
    """
    sky130A sky130_fd_sc_hs OpenLANE Flow
    ------------------

    **NOTE:** See https://github.com/The-OpenROAD-Project/OpenLane#setting-up-openlane for
    information on how to setup OpenLANE and the sky130 PDK.

    For information the the possible `flow_settings` that can be set, see
    https://openlane.readthedocs.io/en/latest/configuration/README.html#variables-information

    Required tools:
        * ``openlane``
        * ``docker``

    Build products:
        * ``config.tcl``: OpenLANE configuration script.
        * ``{{name}}.sdc``: Clock constraints.
        * ``{{name}}.v``: Design verilog
        * ``{{name}}.debug.v``: Design debug verilog
        * ``runs/*``: OpenLANE flow output

    """
    pdk = "sky130A"
    cell_library = "sky130_fd_sc_hs"

class Sky130MediumSpeedPlatform(OpenLANEPlatform):
    """
    sky130A sky130_fd_sc_ms OpenLANE Flow
    ------------------

    **NOTE:** See https://github.com/The-OpenROAD-Project/OpenLane#setting-up-openlane for
    information on how to setup OpenLANE and the sky130 PDK.

    For information the the possible `flow_settings` that can be set, see
    https://openlane.readthedocs.io/en/latest/configuration/README.html#variables-information

    Required tools:
        * ``openlane``
        * ``docker``

    Build products:
        * ``config.tcl``: OpenLANE configuration script.
        * ``{{name}}.sdc``: Clock constraints.
        * ``{{name}}.v``: Design verilog
        * ``{{name}}.debug.v``: Design debug verilog
        * ``runs/*``: OpenLANE flow output

    """
    pdk = "sky130A"
    cell_library = "sky130_fd_sc_ms"

class Sky130LowSpeedPlatform(OpenLANEPlatform):
    """
    sky130A sky130_fd_sc_ls OpenLANE Flow
    ------------------

    **NOTE:** See https://github.com/The-OpenROAD-Project/OpenLane#setting-up-openlane for
    information on how to setup OpenLANE and the sky130 PDK.

    For information the the possible `flow_settings` that can be set, see
    https://openlane.readthedocs.io/en/latest/configuration/README.html#variables-information

    Required tools:
        * ``openlane``
        * ``docker``

    Build products:
        * ``config.tcl``: OpenLANE configuration script.
        * ``{{name}}.sdc``: Clock constraints.
        * ``{{name}}.v``: Design verilog
        * ``{{name}}.debug.v``: Design debug verilog
        * ``runs/*``: OpenLANE flow output

    """
    pdk = "sky130A"
    cell_library = "sky130_fd_sc_ls"

class Sky130HighDensityLowLeakagePlatform(OpenLANEPlatform):
    """
    sky130A sky130_fd_sc_hdll OpenLANE Flow
    ------------------

    **NOTE:** See https://github.com/The-OpenROAD-Project/OpenLane#setting-up-openlane for
    information on how to setup OpenLANE and the sky130 PDK.

    For information the the possible `flow_settings` that can be set, see
    https://openlane.readthedocs.io/en/latest/configuration/README.html#variables-information

    Required tools:
        * ``openlane``
        * ``docker``

    Build products:
        * ``config.tcl``: OpenLANE configuration script.
        * ``{{name}}.sdc``: Clock constraints.
        * ``{{name}}.v``: Design verilog
        * ``{{name}}.debug.v``: Design debug verilog
        * ``runs/*``: OpenLANE flow output

    """

    pdk = "sky130A"
    cell_library = "sky130_fd_sc_hdll"
