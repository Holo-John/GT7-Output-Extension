
"""
GT7 Export Extension for Inkscape - Export Simplified SVG 1.1 for the Gran Turismo 7 Livery Editor
Copyright (C) 2026  HoloJohn (holojohn@gmx.de)

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import traceback
import re
import copy
import inkex
import inkex.command
from inkex import LinearGradient, RadialGradient, MeshGradient, Defs
from inkex import Rectangle, Ellipse, Circle
from inkex import Group, PathElement, Vector2d, Stop, Pattern, ClipPath
from inkex.paths import Path
from inkex.styles import Style
from inkex.transforms import Transform
from inkex.colors import Color
from inkex.elements._base import BaseElement
import sys
import inspect
import os
import tempfile
import logging
import io
from datetime import datetime
import math
import shutil
from typing import Any, Tuple, Protocol, Callable, Iterator, Set, TypedDict
from typing import overload, List, Optional, Iterable, Sequence, cast
from dataclasses import dataclass

LOG_LEVEL = logging.DEBUG

# region --- Path Command ---

# Defined for linting purposes only
class PathCmd(Protocol):
    """
    Structural protocol describing the minimal interface required
    from an Inkex path command for tangent evaluation and endpoint access.

    This protocol intentionally avoids depending on specific Inkex
    command subclasses (Line, Curve, Arc, Close, etc.) and instead
    defines only the attributes and methods your geometry pipeline
    actually uses.

    Attributes
    ----------
    letter : str
        The SVG path command letter ('M', 'L', 'C', 'Q', 'A', 'Z', ...).
        After normalization, this is always uppercase.

    end : Vector2d | None
        The geometric endpoint of the command. For most commands this is
        a Vector2d, but for 'Z' (closepath) it may be None until resolved.

    args : list[float]
        Raw numeric arguments of the command (e.g., control points,
        radii, rotation, flags). Normalized commands always store these
        as floats.

    Methods
    -------
    unit_tangent(first, prev, prev_prev, t) -> Any
        Return the unit tangent vector at parameter t along the command.
        Inkex returns a Vector2d, but we accept Any here because the
        evaluator immediately converts it to a complex number.
    """
     
    letter: str
    end: Vector2d | None
    args: list[float]

    def unit_tangent(
        self,
        first: complex,
        prev: complex,
        prev_prev: complex,
        t: float
    ) -> Any: ...

# endregion

# region --- NullWriter ---
class NullWriter:
    """
    Minimal file-like sink object that silently discards all output.

    This class is used as a stand-in for a writable stream when you want
    to suppress logging or debug output entirely. It implements the two
    methods expected by Python's I/O protocol (`write` and `flush`) but
    performs no action.

    Typical use case
    ----------------
    Attach an instance of `NullWriter` to a logging.StreamHandler to
    prevent Inkscape from printing debug messages to stdout/stderr.

    Methods
    -------
    write(*args, **kwargs)
        Accepts arbitrary data and ignores it.
    flush()
        Required by the stream interface; does nothing.
    """
    def write(self, *args, **kwargs):
        pass
    def flush(self):
        pass

# endregion

class GT7Output(inkex.OutputExtension):
    """
    Inkscape “Save As → GT7 SVG” output extension.

    This extension converts an arbitrary Inkscape SVG 2.0 document into a
    Gran Turismo 7-compatible subset of SVG 1.1 by performing a sequence of
    structural, geometric, and stylistic normalizations. The goal is to
    produce a minimal, flat, attribute-only SVG that GT7's livery editor
    can parse without rejecting or even failing on unsupported constructs.

    Processing pipeline
    -------------------
    The `save()` method orchestrates the full export pipeline, which
    typically includes:

    1. **Preprocessing**
       - Unlink clones
       - Convert text and glyph objects to paths
       - Remove comments
       - Normalize styles into plain presentation attributes
       - Normalize all units to pixels
       - Remove invisible geometry

    2. **Geometry resolution phase I**
       - Remove invisible geometry
       - Replace unsupported objects by paths

    3. **Expand all uses**
        - Clone geomatry referecned via "uses" and inline in the SVG DOM

    4. **Reference resolution**
       - Resolve `clipPath`, `mask`, `filter`, `marker`, and paint servers (gradients, patterns) and convert them into plain geometry or simplify to GT7 requirements (LinearGradient, RadialGradient)
       - NOTE: mask and filter elemnts are discarded, as a conversion to SVG 1.1 is not reasonable
       - Inline referenced nodes where required
       - Remove unsupported references

    5. **Geometry normalization phase II**
       - Replace unsupported shapes with path equivalents
       - Flatten all group transforms into path geometry and remove all groups
       - Translate viewBox to a GT7-safe coordinate system

    6. **Attribute cleanup**
       - Remove unsupported SVG attributes and elements
       - Normalize stroke/fill defaults
       - Strip metadata and editor-specific attributes

    7. **Output compression**
       - Minify numeric values (controlled by rounding_precision parameter)
       - Remove redundant whitespace (controlled by compress-ouput parameter)
       - Group neighouring elements with same presentation attributes and push attributes to group to reduce file size (controlled by compress-output parameter)
       - Compact `<defs>` section or entirely remove it if empty


    Logging
    -------
    The extension writes a detailed changelog to a `gt7_output_extension.log` file in the systems TEMP folder. 
    Logging can slow down the script and is controlled by log_level parameter.

    Internal structure
    ------------------
    The class is organized into helper methods grouped by responsibility:

    - **Style resolution**: `resolve_styles_to_attributes()`
    - **Reference resolution**: `resolve_references()`
    - **Geometry transforms**: `apply_all_transforms()`
    - **Shape replacement**: `replace_unsupported_shapes()`
    - **Group flattening**: `remove_all_groups()`
    - **Stroke cleanup**: `clean_stroke_attributes()`
    - **Output compression**: `compress_output()`
    - **Defs cleanup**: `cleanup_defs()`

    Each helper method performs a single, well-defined transformation on
    the document tree. The pipeline is intentionally linear and
    deterministic to simplify debugging and ensure reproducible output.

    Notes
    -----
    - This extension assumes the document has already been normalized by
      Inkscape's internal parser.
    - All geometry is converted to absolute coordinates.
    - Only GT7-safe attributes remain in the final output. This means the conversion
      cannot preserver the original appearance of all SVG 2.0 files. Always keep a
      backup in the default SVG format!
    """
    
    # region constants

    """
    Log level applied if no log_level parameter is given.
    """
    DEFAULT_LOG_LEVEL = logging.DEBUG

    """
    Name of the log file.
    """
    LOG_FILE = "gt7_output_extension.log"

    SVG_IDENTIFIER_START = re.compile(r"[A-Za-z_]")
    SVG_IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_.:-]*$')
    HEX_COLOR = re.compile(r'^#?(?P<hex>[0-9A-Fa-f]{3}|[0-9A-Fa-f]{4}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$')
    PAINT_ORDER_RE = re.compile(r"\b(stroke|fill|markers)\b")
    CSS_RULE = re.compile(
        r"(?P<selectors>[^{]+)\s*\{\s*(?P<body>[^}]*)\}",
    re.DOTALL
    )
    STRIP_WHITESPACE_AFTER_CMD_LETTERS = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])\s+")
    STRIP_WHITESPACE_EXCEPT_BETWEEN_ARC_CMD = re.compile(r"(?<![01])\s+(?![01]\s)")

    """
    Set of attributes supported by GT7 Livery Editor
    """
    GT7_ATTRS = {
        "id",
        "d",
        "x", "y",
        "cx", "cy",
        "r", "rx", "ry",
        "width", "height",
        "transform",

        # Paint
        "fill",
        "stroke",
        "stroke-width",

        # Paint rules
        "fill-rule",
        "stroke-linecap",
        "stroke-linejoin",
        "stroke-miterlimit",

        # Opacity
        "opacity",
        "fill-opacity",
        "stroke-opacity",
    }

    
    STROKE_ATTRS = [
        "stroke",
        "stroke-width",
        "stroke-linecap",
        "stroke-linejoin",
        "stroke-miterlimit",
        "stroke-dasharray",
        "stroke-dashoffset",
        "stroke-opacity",
    ]

    PRESENTATION_ATTRS = {
        "fill", "fill-opacity", "stroke", "stroke-width", "stroke-opacity",
        "stroke-linecap", "stroke-linejoin", "stroke-miterlimit",
        "opacity", "clip-path", "mask", "filter", "fill-rule",
        "stop-color", "stop-opacity", "paint-order",
        "marker-start", "marker-mid", "marker-end",
    }


    SKIP_RESOLVE_TAGS = {
        "clipPath",
        "mask",
        "linearGradient",
        "radialGradient",
        "pattern",
        "marker",
        "filter",
    }
    

    REFERENCEABLE_TAGS = {
        "clipPath",
        "mask",
        "filter",
        "pattern",
        "linearGradient",
        "radialGradient",
        "symbol",
        "marker"
    }

    XLINK_NS = "http://www.w3.org/1999/xlink"

    REF_ATTRS = (
        "clip-path", "mask", "filter",
        "fill", "stroke",
        "marker-start", "marker-mid", "marker-end",
        "href", f"{{{XLINK_NS}}}href"
    )

    # endregion

    # region --- inkex.OutputExtension interface ---

    def __init__(self):
        """
        Initialize the GT7 output extension.

        This constructor sets up internal state used throughout the export
        pipeline, including ID generation and a two-tier logging system 
        (null stream + optional file log).

        The logger is intentionally configured to be silent by default:
        all messages are routed to a `NullWriter` until a file handler is
        attached via `create_changelog()`. This prevents Inkscape from
        printing debug output to stdout/stderr during intial DOM preparation
        while still allowing detailed logging when exporting a GT7 SVG.

        Attributes
        ----------
        REF_ATTRS : tuple[str, ...]
            SVG attributes that may contain references to other nodes
            (clipPath, mask, filter, paint servers, markers, href).
            These must be inspected and resolved during export.

        id_counters : dict[str, int]
            Per-prefix counters used for fast geenration of stable, collision-free
            IDs when rewriting or inlining referenced nodes.

        logger : logging.Logger
            Internal logger used for structured debug output. Initially
            configured with a `NullWriter` stream handler to suppress
            console output.

        file_handler : logging.FileHandler | None
            Added later by `create_changelog()` to write a detailed
            export log next to the output SVG.

        test_dir : str | None
            Optional directory used for test output or diagnostic dumps.
            Not used in normal operation.
        """
        super().__init__()

        self.id_counters = {}

        # ---------------------------------------------------------
        # Logging setup
        # ---------------------------------------------------------
        self.logger:logging.Logger = logging.getLogger("gt7_output_extension")
        self.logger.setLevel(self.DEFAULT_LOG_LEVEL)
        
        null_handler:logging.Handler = logging.StreamHandler(stream=NullWriter())
        formatter:logging.Formatter = logging.Formatter(
            "[%(levelname)s] %(funcName)s:%(lineno)d: %(message)s"
        )
        null_handler.setFormatter(formatter)
        self.logger.addHandler(null_handler)

        # File handler will be added later in create_changelog()
        self.file_handler = None
        # ---------------------------------------------------------

        self.test_dir = None

    def create_changelog(self, stream):
        """
        Create or reset the file-based changelog for this export run.

        This method determines the appropriate log file path based on the
        output stream, removes any previously attached file handler, and
        attaches a fresh `FileHandler` in overwrite mode. Inkscape keeps
        extension instances alive across multiple runs, so the handler must
        be recreated each time to avoid appending to stale logs or leaking
        file handles.

        Parameters
        ----------
        stream : file-like
            The output stream provided by Inkscape. Its `.name` attribute is
            used to derive the log file location; anonymous or temporary
            streams fall back to a log file in the system temp directory.

        Side Effects
        ------------
        - Sets `self.log_path` to the resolved log file path.
        - Removes and closes any existing file handler.
        - Attaches a new `FileHandler` for this export run.
        """

        outname = getattr(stream, "name", None)

        if not outname or outname.startswith("<"):
            self.log_path = os.path.join(tempfile.gettempdir(), self.LOG_FILE)
        else:
            self.log_path = outname + ".log"

        # Always recreate the file handler (overwrite mode)
        if self.file_handler:
            self.logger.removeHandler(self.file_handler)
            self.file_handler.close()
            self.file_handler = None

        file_handler = logging.FileHandler(self.log_path, mode="w", encoding="utf-8")
        formatter = logging.Formatter(
            "[%(levelname)s] %(funcName)s(%(lineno)d): %(message)s"
        )
        file_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)
        self.file_handler = file_handler


    def add_arguments(self, pars):
        """
        Declare extension parameters for Inkscape's Save-As dialog.

        These arguments are exposed to the user through the `.inx` file and
        automatically populated into `self.options` when the exporter runs.
        They control numeric precision, mesh-gradient sampling density, output
        compression, alpha-stripping behavior, and log verbosity.

        Parameters
        ----------
        pars : inkex.ArgumentParser
            The parser used by Inkscape to construct the parameter dialog and
            populate `self.options` with the values selected by the user.

        Notes
        -----
        All parameters must be declared here for Inkscape to accept them (see corresponding .inx file).
        `inkex.Boolean` is used for checkbox fields; integer and string types are passed through directly.
        """

        pars.add_argument("--strip_alpha", type=inkex.Boolean, default=True) # type: ignore
        pars.add_argument("--rounding_precision", type=int, default=3)
        pars.add_argument("--mesh_divisions", type=int, default=16)
        pars.add_argument("--compress_output", type=inkex.Boolean, default=False) # type: ignore
        pars.add_argument("--log_level", type=str, default=False) # type: ignore
        
    def effect(self):
        """
        Defined by the Inkscape extension API but unused for output extensions.

        `OutputExtension` does not invoke `effect()`. All export logic is
        performed in `save()`, so this method is intentionally left empty.
        """
        pass

    def save(self, stream):
        """
        Execute the full GT7 export pipeline and write the final SVG to the
        provided output stream.

        This method is the main entry point for output generation. It sets up
        logging, captures Inkscape's stdout/stderr noise, performs all document
        normalization steps, resolves geometry and references, flattens the SVG
        DOM, applies GT7-specific cleanup rules, and finally serializes the
        resulting SVG in either text or binary mode depending on the stream
        type.

        Parameters
        ----------
        stream : file-like
            The output destination used by Inkscape or test harnesses. Its
            `.name` attribute is also used to determine the changelog path
            (which usually falls back to TEMP directory)

        Notes
        -----
        The pipeline is intentionally linear and deterministic. Each stage
        modifies the document in place and may emit diagnostic snapshots via
        `log_svg()` when tracing is enabled. Any exception is logged and then
        re-raised to allow Inkscape to report the failure.
        """

        try:
            self.create_changelog(stream)

            # Redirect stdout and stderr
            _stdout_inkscape = sys.stdout
            _stderr_inkscape = sys.stderr
            _stdout_capture = io.StringIO()
            _stderr_capture = io.StringIO()
            sys.stdout = _stdout_capture
            sys.stderr = _stderr_capture

            self.preprocess(types_to_path=["text"], unlink_clones=True)

            sys.stderr = _stderr_inkscape
            sys.stdout = _stdout_inkscape

            self.init_log_level()

            self.log(logging.INFO, """GT7 Export Extension  Copyright (C) 2026  HoloJohn (holojohn@gmx.de)
This program comes with ABSOLUTELY NO WARRANTY.
This is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License version 3.
See https://www.gnu.org/licenses/gpl-3.0.html for details.
""")
        
            self.log(logging.INFO, f"Started @ {datetime.now().isoformat()}")
            self.log(logging.INFO, f"Python executable: {sys.executable}")
            self.log(logging.INFO, f"inkex loaded from: {inspect.getfile(inkex)}")
            self.log(logging.INFO, f"inkex version: {getattr(inkex, '__version__', 'NO VERSION ATTRIBUTE')}")
            self.log(logging.INFO, f"compress ouput: {self.options.compress_output}")
            self.log(logging.INFO, f"rounding precision: {self.options.rounding_precision}")
            self.log(logging.INFO, f"mesh divisions: {self.options.mesh_divisions}")
            self.log(logging.INFO, f"strip alpha: {self.options.strip_alpha}")
            self.log(logging.INFO, f"tracing: {self.options.log_level}")

            capture = _stderr_capture.getvalue().strip()
            if len(capture) > 0:
                self.log(logging.WARNING, f"Inkscape: {capture}")
            
            self.remove_comments()
            self.resolve_css_classes()
            self.resolve_styles_to_attributes()
            self.log_svg(header="AFTER resolve_styles_to_attributes()")

            self.normalize_units()
            self.log_svg(header="AFTER normalize_units()")

            self.expand_all_uses()
            self.log_svg(header="AFTER expand_all_uses()")

            self.resolve_geometry()
            self.log_svg(header="AFTER resolve_geometry()")

            self.resolve_references()
            self.log_svg(header="AFTER resolve_references()")

            self.flatten_svg_dom()
            self.log_svg(header="AFTER flatten_svg_dom()")
            
            self.translate_viewbox()
            self.log_svg(header="AFTER translate_viewbox()")

            self.sweep_svg_tree()
            self.log_svg(header="AFTER sweep_svg_tree()")

            svg_bytes = inkex.etree.tostring(
                self.svg,
                encoding="utf-8",
                xml_declaration=True,
            )
            #svg_bytes = re.sub(rb"\s+", b" ", svg_bytes)
            
            if isinstance(stream, io.TextIOBase):
                # pytest / text mode
                stream.write(svg_bytes.decode("utf-8"))
            else:
                # Inkscape / binary mode
                stream.write(svg_bytes)
            
        except Exception as e:
            self.log(logging.ERROR,str(e))
            self.log(logging.ERROR,traceback.format_exc())
            raise

    # endregion

    # region --- logging ---

    def init_log_level(self) -> int|None:
        """
        Initialize the logger's effective log level based on the user-selected
        `log_level` option.

        The method maps the string value from the extension parameters to the
        corresponding `logging` constant, applies it to the internal logger, and
        supports a special `"DISABLED"` mode that fully suppresses logging by
        disabling the logger. If the value is unrecognized, the extension's
        default log level (DEBUG) is used.

        Returns
        -------
        int | None
            The resolved logging level constant, or ``None`` if logging is
            disabled.
        """


        str_level = self.options.log_level

        match(str_level):
            case "ERROR":
                level = logging.ERROR
            case "WARNING":
                level = logging.WARNING
            case "INFO":
                level = logging.INFO
            case "DEBUG":
                level = logging.DEBUG
            case "DISABLED":
                level = None
            case _:
                level = self.DEFAULT_LOG_LEVEL

        self.logger.disabled = level is None

        if level is not None:
            self.logger.setLevel(level)

        return level


    def log(self, level:int, msg:str, stacklevel:int=2) -> None:
        """
        Unified logging wrapper that mirrors messages to both the internal
        logger and the extension's message channel.

        This method provides consistent logging behavior across the GT7 export
        pipeline. Error and warning messages are always forwarded to
        `self.msg()` so they appear in Inkscape's error dialog, while all
        messages are conditionally emitted through the internal `logging.Logger`
        depending on the active log level. Caller information is preserved via
        the `stacklevel` parameter.

        Parameters
        ----------
        level : int
            Logging severity (e.g., ``logging.INFO``). Determines whether the
            internal logger will emit the message.
        msg : str
            The message to log.
        stacklevel : int, default 2
            Stack offset used to report the correct caller location in the
            internal logger.
        """

        if level == logging.ERROR:
            self.msg(f"[ERROR] {msg}")
            
        elif level == logging.WARNING:
            self.msg(f"[WARNING] {msg}")

        if self.logger.isEnabledFor(level):
            self.logger.info(msg, stacklevel=stacklevel)


    def log_svg(self, node:BaseElement|None=None, indent:int=0, header:str="SVG DOM") -> None:
        """
        Recursively log the SVG DOM subtree with indentation, producing a
        readable, XML-like outline of the current document state.

        This diagnostic helper is used throughout the GT7 export pipeline to
        emit structural snapshots after major normalization steps. Each node is
        printed either as a self-closing tag when it has no children, or as a
        paired opening/closing tag when children are present. Attributes are
        rendered inline, and indentation reflects DOM depth.

        Parameters
        ----------
        node : BaseElement | None
            The element to start logging from. If ``None``, the document root
            (`self.svg`) is used.
        indent : int
            Current indentation level, increased for each recursive descent.
        header : str
            Optional section header printed before and after the tree dump.
        """


        if header:
            self.log(logging.DEBUG, f"-------------------- {header} --------------------")

        if node is None:
            node = self.svg

        tag = self.tag_name(node)
        attrs = " ".join(f"{k}='{v}'" for k, v in node.attrib.items())
        pad = "  " * indent

        # --- 1. Detect children ---
        children = list(node)

        # --- 2. Opening tag ---
        if children:
            # Node has children → normal opening tag
            if attrs:
                self.log(logging.DEBUG, f"{pad}<{tag} {attrs}>", stacklevel=3)
            else:
                self.log(logging.DEBUG, f"{pad}<{tag}>", stacklevel=3)

            # --- 3. Recurse into children ---
            for child in children:
                self.log_svg(child, indent + 1, header="")

            # --- 4. Closing tag ---
            self.log(logging.DEBUG, f"{pad}</{tag}>", stacklevel=3)

        else:
            # Node has no children → self-closing tag
            if attrs:
                self.log(logging.DEBUG, f"{pad}<{tag} {attrs} />", stacklevel=3)
            else:
                self.log(logging.DEBUG, f"{pad}<{tag} />", stacklevel=3)

        if header:
            self.log(logging.DEBUG, f"-----------------------------------------------")

    # endregion

    # region --- DOM Tree and SVG Helpers ---
    
    def ensure_defs(self) -> Defs:
        """
        Returns the <defs> element or creates one if not existing and returns it.

        This helper searches the SVG root for an existing <defs> section and
        returns it if found. If no <defs> element is present, a new one is
        created using the document's own SVG namespace and inserted at the
        top level of the DOM. Many GT7-normalization steps rely on <defs> as a
        stable location for reusable resources, so this method guarantees that
        the element always exists.

        Returns
        -------
        inkex.Defs
            The existing or newly created <defs> element.
        """

        root = self.svg

        # Find existing <defs>
        defs = root.find(".//{http://www.w3.org/2000/svg}defs")
        if defs is not None:
            return defs

        # Create new <defs> using the document's own SVG namespace
        svg_ns = root.nsmap.get(None, "http://www.w3.org/2000/svg")
        defs = inkex.etree.Element(f"{{{svg_ns}}}defs")

        # Insert at top of document
        self.add_node(defs, root, 0)
        return defs


    def local_transform(self, node:BaseElement) -> Transform:
        """
        Return the node's local transform as a `Transform` object.

        This helper reads the element's `transform` attribute and converts it
        into an `inkex.Transform`. If the attribute is absent, an identity
        transform is returned. The result represents only the node's own
        transform, without any inherited group transforms.
        """

        tstr = node.get("transform", None)
        return Transform(tstr) if tstr else Transform()

    
    def generate_id(self, el:BaseElement) -> str:
        """
        Generate a short, collision-free ID for the given element.

        The ID consists of the uppercase first letter of the element's tag
        followed by a monotonically increasing counter (e.g., ``P1``, ``L2``).
        Uniqueness is ensured by checking the DOM via ``find_node()``; if the
        candidate ID already exists, the counter is incremented until a free
        identifier is found. The final ID is written to the element and also
        returned.

        Parameters
        ----------
        el : BaseElement
            The SVG element that should receive a generated ID.

        Returns
        -------
        str
            The newly assigned unique ID.
        """


        tag = self.tag_name(el) or "X"
        prefix = tag[0].upper()

        # counter per prefix
        count = self.id_counters.setdefault(prefix, 0)

        while True:
            count += 1
            candidate = f"{prefix}{count}"

            # ensure uniqueness in DOM
            if self.find_node(candidate) is None:
                break

        self.id_counters[prefix] = count
        el.set("id", candidate)
        return candidate


    @overload
    def node_or_id_to_url(self, id_or_node: str) -> str: ...
    @overload
    def node_or_id_to_url(self, id_or_node: int) -> str: ...
    @overload
    def node_or_id_to_url(self, id_or_node: BaseElement) -> str: ...

    def node_or_id_to_url(self, id_or_node: Any) -> str:
        """
        Convert a node or raw ID into a `url(#id)` reference.

        This helper accepts either a string ID, an integer ID, or an actual SVG
        element. String and integer inputs are treated as literal IDs. When a
        `BaseElement` is provided, its `id` attribute is read and used to form
        the URL; if the element has no ID, the fallback ``url(#none)`` is
        returned. Any other input type is rejected.

        Parameters
        ----------
        id_or_node : str | int | BaseElement
            The identifier or element to convert into a paint-server URL.

        Returns
        -------
        str
            A string of the form ``url(#ID)`` suitable for use in fill, stroke,
            marker, or other reference attributes.

        Raises
        ------
        ValueError
            If the argument is neither a valid ID nor an SVG node.
        """

        # Case 1: inkex node → use its id attribute
        if isinstance(id_or_node, str):
            return f"url(#{id_or_node})"

        if self.is_svg_node(id_or_node):
            node_id = id_or_node.get("id")
            return f"url(#{node_id})" if node_id else "url(#none)"

        raise ValueError(f"str(id_or_node) is no valid SVG node nor ID")


    def url_to_id(self, url:str) -> str|None:
        """
        Extract a valid SVG ID from a `url(#id)` reference.

        This helper normalizes paint-server references by stripping the `url(...)`
        wrapper, removing optional quotes, and validating the remaining fragment
        as a legal SVG identifier. Only `url(#id)` forms are accepted; bare
        strings or non-ID references return ``None``. Validation prefers a full
        ID regex (`SVG_IDENTIFIER_RE`) when available, falling back to the
        single-character start check (`SVG_IDENTIFIER_START`) used elsewhere in
        the GT7 pipeline.

        Parameters
        ----------
        url : str
            A reference such as ``url(#grad1)`` or ``#foo``. The
            value is trimmed and case-normalized before processing.

        Returns
        -------
        str | None
            The extracted ID if valid, otherwise ``None``.
        """

        if not url:
            return None

        url = url.strip()

        if url.lower() == "none":
            return None

        # Extract raw reference target (case-insensitive "url(")
        if url.lower().startswith("url(") and url.endswith(")"):
            url = url[4:-1].strip().strip('\'"')   # remove surrounding quotes if any

        if url.startswith("#"):
            candidate = url[1:]
        else:
            return None

        # Validate candidate as a legal SVG ID (validate whole string)
        # Prefer a full-ID regex; fall back to your single-char check if needed
        if candidate and getattr(self, "SVG_IDENTIFIER_RE", None):
            if self.SVG_IDENTIFIER_RE.match(candidate):
                return candidate
        else:
            # fallback to your existing start-char check
            if candidate and self.SVG_IDENTIFIER_START.match(candidate[0]):
                return candidate

        return None


    """
    Resolve a reference attribute into its target node and ID.

    This helper reads an element's reference attribute (default ``href``),
    normalizes it via ``url_to_id()``, and attempts to locate the referenced
    node in the DOM. If the reference is stale, the attribute is reset to
    ``none`` and a debug message is logged. Optional tag filtering allows the
    caller to restrict valid targets to a specific tag or set of tags.

    Parameters
    ----------
    el : BaseElement
        The element containing the reference attribute.
    attr : str
        The attribute name to inspect (e.g., ``"href"``, ``"clip-path"``).
    tag_name : str | Iterable[str] | None
        Optional tag or set of tags that the referenced node must match.

    Returns
    -------
    tuple[BaseElement | None, str | None]
        The resolved node and its ID, or ``(None, None)`` if the reference is
        missing, invalid, stale, or does not match the required tag(s).
    """
    def ref_target(self, el:BaseElement, attr:str="href", tag_name:str|Iterable[str]|None=None) -> tuple[Optional[BaseElement], Optional[str]]:
        href = el.get(attr) or el.get(f"{{{self.XLINK_NS}}}{attr}")
        if href is None:
            return None, None

        id = self.url_to_id(href)
        if id is None:
            return None, None
        
        node = self.find_node(id)
        if node is None:

            if not self.HEX_COLOR.fullmatch(id):
                el.set(attr, "none")
                self.log(logging.DEBUG, f"Removing stale id='{id}' from {self.node_str(el)}")

            return None, None

        if not tag_name is None:
            tag = self.tag_name(node)

            if isinstance(tag_name, str):
                if  tag != tag_name:
                    return None, None
            else:
                tag_names = set(tag_name)

                if tag not in tag_names:
                    return None, None

        return node, id
 

    def parent_of(self, child:BaseElement) -> Tuple[Optional[BaseElement], int]:
        """
        Return the parent of the given node and its index within that parent.
    
        This helper wraps `getparent()` and provides a consistent tuple form
        used throughout the GT7 pipeline. If the node has no parent (e.g., it is
        the document root), the method returns ``(None, 0)``. Otherwise it
        returns the parent element together with the child's positional index in
        that parent's element list.
    
        Parameters
        ----------
        child : BaseElement
            The node whose parent relationship should be resolved.
    
        Returns
        -------
        tuple[BaseElement | None, int]
            The parent element and the child's index, or ``(None, 0)`` if the
            node has no parent.
        """

        parent = child.getparent()
        if parent is None:
            return None, 0
        return parent, parent.index(child)

    
    def find_node(self, id: str) -> BaseElement | None:
        """
        Find an SVG node by its ID, using both fast lookup and XPath fallback.

        This helper first attempts `getElementById()`, which is efficient but
        not always reliable in Inkscape's DOM. If that fails, it performs an
        XPath search for any element whose `id` attribute matches the requested
        value. When no node is found and the ID is not a hexadecimal color
        literal, a debug message is logged to aid tracing of stale or broken
        references.

        Parameters
        ----------
        id : str
            The ID to search for in the SVG document.

        Returns
        -------
        BaseElement | None
            The matching element, or ``None`` if no such node exists.
        """

        node = self.svg.getElementById(id)

        if node is None:
            result = self.svg.xpath(f"//*[@id='{id}']")
            node = result[0] if result else None

        if node is None and not self.HEX_COLOR.fullmatch(id):
            self.log(logging.DEBUG, f"Node with id='{id}' not found")

        return node


    def node_str(self, node:BaseElement) -> str:
        """
        Return a compact string representation of an SVG node for debugging and tracing.

        This helper formats the element as
            <tag, id=..., attrib={...}>
        using the node's tag name, its `id` attribute, and a shallow copy of its
        attribute dictionary. It is primarily used in log messages to provide a
        concise, human-readable identifier for nodes during DOM normalization
        and reference resolution.

        Parameters
        ----------
        node : BaseElement
            The SVG element to stringify. If ``None`` is passed, the literal
            string ``"None"`` is returned.

        Returns
        -------
        str
            A short descriptive string representing the node.
        """

        if node is None:
            return "None"
        else:
            return f"<{self.tag_name(node)}, id={node.get("id")}, attrib={dict(node.attrib)}>"


    def add_node(self, node:BaseElement, parent:BaseElement, index:int|None=None) -> BaseElement:
        """
        Insert a node into the DOM, assign deterministic IDs, and sanitize
        forbidden ID cases.

        This helper centralizes all DOM-mutation rules used in the GT7 export
        pipeline. It inserts the given node into its parent (either appended or
        placed at a specific index), ensures that the node receives a valid,
        unique ID unless it is a `<stop>` or `<defs>` element, and recursively
        assigns IDs to all descendants except `<stop>` nodes. Any pre-existing
        ID is preserved but logged for traceability. The method guarantees that
        newly introduced elements always conform to the exporter's ID policy,
        preventing Inkscape from generating long or unstable identifiers.

        Parameters
        ----------
        node : BaseElement
            The element to insert and normalize.
        parent : BaseElement
            The parent element into which the node will be inserted.
        index : int | None
            Optional insertion index. If omitted, the node is appended.

        Returns
        -------
        BaseElement
            The inserted node, after ID assignment and descendant normalization.
        """

        tag = node.tag.split('}')[-1]

        # ---------------------------------------------------------
        # Strip STOP IDs immediately
        # ---------------------------------------------------------
        if tag in { "stop", "defs" }:
            node.attrib.pop("id", None)
        else:
            # ---------------------------------------------------------
            # Assign ID to gradients, paths, groups, etc.
            # ---------------------------------------------------------
            if not node.get("id"):
                new_id = self.generate_id(node)
                node.set("id", new_id)
            else:
                self.log(logging.DEBUG, f"[ADD_NODE]   existing id={node.get('id')} on <{tag}>")

        # ---------------------------------------------------------
        # Insert node
        # ---------------------------------------------------------
        if index is None:
            parent.append(node)
            self.log(logging.DEBUG, f"[ADD_NODE]   appended <{self.node_str(node)}>")
        else:
            parent.insert(index, node)
            self.log(logging.DEBUG, f"[ADD_NODE]   inserted <{self.node_str(node)}> at index={index}")

        # ---------------------------------------------------------
        # Assign IDs to descendants (except stops)
        # ---------------------------------------------------------
        for el in node.iter():
            etag = el.tag.split('}')[-1]
            if etag == "stop":
                el.attrib.pop("id", None)
                continue

            if not el.get("id"):
                new_id = self.generate_id(el)
                el.set("id", new_id)
                self.log(logging.DEBUG, f"[ADD_NODE]   descendant <{etag}> assigned id={new_id}")

        return node


    def remove_node(self, node:BaseElement, parent:BaseElement|None=None) -> None:
        """
        Remove a node from the DOM and log the operation.

        This helper detaches the given element from its parent, using either the
        explicitly supplied parent or the node's own `getparent()` result. If
        the node is attached, it is removed and a debug message is emitted that
        includes both the node's tag/id and a compact representation of the
        parent. No action is taken when the node has no parent.

        Parameters
        ----------
        node : BaseElement
            The element to remove from the DOM.
        parent : BaseElement | None
            Optional explicit parent. If omitted, the node's actual parent is
            used.

        Returns
        -------
        None
            The method performs a side-effect only (DOM mutation + logging).
        """

        if parent is None:
            parent = node.getparent()
        if parent is not None:
            parent.remove(node)

            self.log(logging.DEBUG, f"[REMOVE_NODE] removed <{self.tag_name(node)}, id='{node.get('id')}'> from parent <{self.node_str(parent)}>") 


    def inherit_attribute(self, node:BaseElement, attr:str) -> str|None:
        """
        Walk up the DOM tree and return the nearest value for the given
        presentation attribute.

        This helper implements simple inheritance for attributes that SVG allows
        to propagate through ancestor elements (e.g., `fill`, `stroke`,
        `opacity`). Starting from the given node, it checks whether the attribute
        is present in the element's own `attrib` dictionary; if not, it climbs
        the parent chain until a match is found. If no ancestor provides the
        attribute, the method returns ``None``.

        Parameters
        ----------
        node : BaseElement
            The element from which to begin the lookup.
        attr : str
            The attribute name to search for.

        Returns
        -------
        str | None
            The inherited attribute value, or ``None`` if no ancestor defines it.
        """

        while node is not None:
            if attr in node.attrib:
                return node.attrib[attr]
            node = node.getparent()
        return None

            
    def tag_name(self, el:BaseElement) -> str:
        """
        Return the local tag name of an SVG element without its namespace.

        This helper extracts the bare tag from an element whose fully qualified
        name may include an XML namespace (e.g., ``{http://www.w3.org/2000/svg}path``).
        If the tag is not a string—an edge case that can occur with certain
        Inkscape-generated nodes—the method returns an empty string. Otherwise,
        the namespace prefix is stripped and only the final tag component is
        returned.

        Parameters
        ----------
        el : BaseElement
            The SVG element whose tag name should be normalized.

        Returns
        -------
        str
            The tag name without namespace, such as ``"path"`` or ``"linearGradient"``.
        """

        tag = el.tag
        if not isinstance(tag, str):
            return ""
        return tag.split("}")[-1]


    def is_svg_node(self, node:BaseElement) -> bool:
        """
        Return True only for genuine SVG element nodes.

        This predicate filters out all non-element DOM objects that may appear
        in Inkscape's tree: comments, text nodes, processing instructions, and
        accidental callables. It relies on `tag_name()` to verify that the node
        has a real string tag and strips namespaces before checking. The method
        also requires the presence of an `attrib` dictionary, ensuring the node
        behaves like an Inkex/lxml element.

        Parameters
        ----------
        node : BaseElement
            The DOM object to test.

        Returns
        -------
        bool
            True if the object is a real SVG element; False otherwise.
        """

        # 1. Must have a valid tag name
        local = self.tag_name(node)
        if not local:
            return False

        # 2. Must have attributes (Inkex or lxml element)
        if not hasattr(node, "attrib"):
            return False

        # 3. Must not be callable (your earlier bug)
        if callable(node):
            return False

        return True


    def is_expandable_ref(self, ref_el:BaseElement)-> bool:
        """
        Return True if the referenced element is eligible for expansion.

        This predicate is used during reference-resolution (e.g., expanding
        <use> elements). It checks the referenced node's tag name against
        `SKIP_RESOLVE_TAGS`, a set of tags that must *not* be expanded (such as
        gradients, clipPaths, or other resource definitions). Any element whose
        local tag name is **not** in that skip-set is considered expandable.

        Parameters
        ----------
        ref_el : BaseElement
            The referenced SVG element to test.

        Returns
        -------
        bool
            True if the element may be expanded; False if its tag is in
            `SKIP_RESOLVE_TAGS`.
        """

        return self.tag_name(ref_el) not in self.SKIP_RESOLVE_TAGS


    def copy_presentation_attributes(self, src:BaseElement, dst:BaseElement|list[BaseElement], override:bool=True)-> None:
        """
        Copy presentation attributes from `src` to `dst`, with optional override
        semantics, and with list-expansion support.

        This helper applies the GT7-safe presentation-attribute propagation
        rules. When `dst` is a list (e.g., a multi-element expansion result), the
        operation is applied to each element. When `dst` is a real SVG node, the
        method iterates over `PRESENTATION_ATTRS` and copies each attribute from
        `src` to `dst` according to the `override` flag:

        - If `override` is True, any attribute present on `src` replaces the value on `dst`.
        - If `override` is False, attributes are only copied when `dst` does not already define them.

        This function is used throughout the `<use>` expansion pipeline and
        pattern/gradient normalization to ensure consistent, deterministic
        styling behavior.

        Parameters
        ----------
        src : BaseElement
            The element providing presentation attributes.
        dst : BaseElement | list[BaseElement]
            The element(s) receiving presentation attributes.
        override : bool
            Whether attributes from `src` should overwrite existing values on
            `dst`.

        Returns
        -------
        None
            The method mutates `dst` in place.
        """

        if isinstance(dst, list):
            for el in dst:
                self.copy_presentation_attributes(src, el, override=override)
                
        elif self.is_svg_node(dst):                
            for attr in self.PRESENTATION_ATTRS:
                if attr in src.attrib and (override or attr not in dst.attrib):
                    dst.set(attr, src.get(attr))

                
    def to_float(self, value:None|str, default:float=0.0) -> float:
        """
        Convert an arbitrary value into a float with robust fallback handling.

        This helper normalizes loosely-typed numeric inputs coming from Inkex or
        lxml attributes. It accepts `None` or any string-convertible value,
        strips whitespace, and attempts `float()` conversion. Empty strings,
        missing attributes, and invalid numeric formats all resolve to the
        provided `default`. Conversion failures are logged with full traceback
        to aid debugging of malformed SVG attributes.

        Parameters
        ----------
        value : None | str
            The raw attribute value to convert. May be `None`, an empty string,
            or any string representation of a number.
        default : float
            The fallback value returned when conversion is impossible.

        Returns
        -------
        float
            The parsed float, or `default` if the input is empty, missing, or
            not convertible.
        """

        if value is None:
            return default
        
        s = str(value).strip()
        if not s:
            return default
        try:
            return float(s)
        except ValueError as e:
            self.log(logging.WARNING, f"Cannot convert {value} to float")
            self.log(logging.WARNING, str(e))
            self.log(logging.WARNING, traceback.format_exc())
            return default
            
    def round_floats_in_string(self, s:str, digits:int=3) -> str:
        """
        Round all floating-point literals inside an arbitrary string.

        This helper scans the input string for any SVG-legal float representation
        and rewrites each match using a fixed number of decimal places, followed
        by removal of trailing zeros and a trailing decimal point. Supported
        formats include:

        - `12.34` (standard decimal)
        - `-12.34` (signed decimal)
        - `12.`   (decimal with empty fractional part)
        - `.34`   (fraction-only form)
        - `1e-3`, `-2.5e+2` (scientific notation)

        The regex is intentionally permissive and matches floats that appear
        anywhere in the string, provided they are not immediately preceded by a
        letter. Each match is converted via Python's `float()` and rounded using
        the specified number of digits. The function is GT7-safe and produces
        minimal, stable float output suitable for SVG attributes.

        Parameters
        ----------
        s : str
            The string to process. May contain arbitrary text and multiple
            numeric fragments.
        digits : int
            Number of decimal places to round to before trimming.

        Returns
        -------
        str
            The transformed string with all float literals rounded and cleaned.
        """

        if not s:
            return ""
        
        # Matches:
        #   12.34
        #   -12.34
        #   12.
        #   .34
        #   1e-3, -2.5e+2  (optional scientific notation)
        float_re = r"""
            (?<![\w])                # no letter before
            -?                       # optional sign
            (?:\d+\.\d*|\.\d+|\d+)   # 12.34 | 12. | .34 | 12
            (?:[eE][+-]?\d+)?        # optional exponent
        """

        def repl(match):
            num = float(match.group())
            rounded = f"{num:.{digits}f}".rstrip("0").rstrip(".")

            # normalize -0 to 0
            if rounded == "-0":
                rounded = "0"

            return rounded

        return re.sub(float_re, repl, s, flags=re.VERBOSE)

            
    def is_geometry(self, el:BaseElement, only_gt7_supported:bool=True) -> bool:
        """
        Identify geometry nodes.

        When `only_gt7_supported` is True (default), the method returns True
        only for GT7-safe geometry types:

            path, rect, circle, ellipse

        When `only_gt7_supported` is False, the method returns True for all
        SVG geometry types that can be converted to <path>:

            path, rect, circle, ellipse, line, polyline, polygon

        This predicate is used throughout the transform pipeline to decide
        whether a node participates in geometry normalization, path
        conversion, boolean operations, and GT7 compliance filtering.
        """

        tag = self.tag_name(el)

        if only_gt7_supported:
            # GT7-safe geometry
            return tag in ("path", "rect", "circle", "ellipse")

        # Full SVG geometry (convertible to <path>)
        return tag in (
            "path",
            "rect",
            "circle",
            "ellipse",
            "line",
            "polyline",
            "polygon"
        )

    # endregion
     
    # region --- Transformation Helpers ---        

    def invert_transform(self, T: Transform) -> Transform:
        """
        Invert an Inkex Transform matrix.

        Inkex stores affine transforms in the form:
            ((a, b, e), (c, d, f))
        representing the 2x3 matrix:

            [ a  b  e ]
            [ c  d  f ]

        This function computes the inverse of that matrix. If the determinant is
        too close to zero (non-invertible transform), an identity Transform is
        returned instead. The output is a new `Transform` constructed from the
        six inverse coefficients in Inkex's expected order.

        Parameters
        ----------
        T : Transform
            The affine transform to invert.

        Returns
        -------
        Transform
            The inverse transform, or an identity transform if the matrix is
            singular.
        """


        # Inkex stores matrix as ((a, b, e), (c, d, f))
        (a, b, e), (c, d, f) = T.matrix

        det = a * d - b * c
        if abs(det) < 1e-12:
            # non-invertible → identity
            return Transform()

        inv_a =  d / det
        inv_b = -b / det
        inv_c = -c / det
        inv_d =  a / det
        inv_e = (b * f - d * e) / det
        inv_f = (c * e - a * f) / det

        return Transform([inv_a, inv_b, inv_c, inv_d, inv_e, inv_f])


    def shape_bbox(self, node:BaseElement) -> None | tuple[float, float, float, float]:
        """
        Return the local bounding box of a single geometry element.

        The result is always a 4-tuple `(x, y, width, height)` in the element's
        **own local coordinate system**, without applying any transforms. This
        is used during `objectBoundingBox → userSpaceOnUse` conversion, pattern
        normalization, and gradient-unit resolution.

        Supported shapes:

        - path      → computed via `inkex.Path(...).bounding_box()`
        - rect      → direct attribute readout
        - circle    → box from center/radius
        - ellipse   → box from center/radii
        - polygon   → min/max of parsed point list
        - polyline  → same as polygon

        If the element has no geometry (missing `d`, empty `points`, etc.) or is
        not one of the supported types, the function returns `None`.

        Returns
        -------
        None | tuple[float, float, float, float]
            The bounding box in local coordinates, or None if unsupported.
        """

        tag = self.tag_name(node)

        # PATH
        if tag == "path":
            d = node.get("d")
            if not d:
                return None
            p = inkex.Path(d)  # type: ignore
            bbox = p.bounding_box()  # returns BoundingBox(x_interval, y_interval)
            if bbox is None:
                return None
            
            min_pt = bbox.minimum   # Vector2d(x_min, y_min)
            max_pt = bbox.maximum   # Vector2d(x_max, y_max)
            return min_pt.x, min_pt.y, max_pt.x - min_pt.x, max_pt.y - min_pt.y

        # RECT
        if tag == "rect":
            x = float(node.get("x") or 0.0)
            y = float(node.get("y") or 0.0)
            w = float(node.get("width") or 0.0)
            h = float(node.get("height") or 0.0)
            return x, y, w, h

        # CIRCLE
        if tag == "circle":
            cx = float(node.get("cx") or 0.0)
            cy = float(node.get("cy") or 0.0)
            r  = float(node.get("r") or 0.0)
            return cx - r, cy - r, 2 * r, 2 * r

        # ELLIPSE
        if tag == "ellipse":
            cx = float(node.get("cx") or 0.0)
            cy = float(node.get("cy") or 0.0)
            rx = float(node.get("rx") or 0.0)
            ry = float(node.get("ry") or 0.0)
            return cx - rx, cy - ry, 2 * rx, 2 * ry

        # POLYGON / POLYLINE
        if tag in ("polygon", "polyline"):
            raw = node.get("points") or ""
            if not raw.strip():
                return None
            coords = [float(v) for v in re.split(r"[ ,]+", raw.strip()) if v]
            pts = [(coords[i], coords[i + 1]) for i in range(0, len(coords), 2)]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)

        # Unsupported shape → return None
        return None

    
    def bbox_transform(self, shape:BaseElement) -> Transform:
        """
        Return a transform that maps the unit box (0,0)-(1,1) onto the shape's
        local bounding box.
    
        This helper is used when converting `objectBoundingBox` units into
        `userSpaceOnUse`. It first computes the shape's local bounding box via
        `shape_bbox()`. If no bounding box exists (unsupported geometry or empty
        data), an identity transform is returned.
    
        For a valid bounding box `(bx, by, bw, bh)`, the resulting transform is:
    
            translate(bx, by) · scale(bw, bh)
    
        which maps the unit square to the shape's actual geometric extent.
    
        Returns
        -------
        Transform
            A transform that scales and translates the unit box to the shape's
            bounding box, or an identity transform if the shape has no geometry.
        """
        bbox = self.shape_bbox(shape)
        if not bbox:
            return Transform()  # identity

        bx, by, bw, bh = bbox
        return Transform(f"translate({bx},{by})") @ Transform(f"scale({bw},{bh})")


    def use_transform(self, use_el:BaseElement) -> Transform:
        """
        Return the effective transform of a <use> element.
    
        A <use> node may contribute two independent transforms:
    
        1. Positional offset via its `x` and `y` attributes.
        2. An explicit `transform="..."` attribute.
    
        This helper converts `x` and `y` into a `translate(tx, ty)` transform
        (using `to_float()` for robust parsing). If the <use> element also has a
        `transform` attribute, that transform is parsed and multiplied with the
        translation:
    
            final = transform · translate
    
        If the transform attribute is invalid or cannot be parsed, a warning is
        logged and only the translation is returned. When both `x` and `y` are
        zero and no transform is present, the identity transform is returned.
    
        Returns
        -------
        Transform
            The combined transform of the <use> element, or identity if none
            applies.
        """
        
        tx = self.to_float(use_el.get("x") or "0")
        ty = self.to_float(use_el.get("y") or "0")
        t_translate = Transform(f"translate({tx},{ty})") if tx or ty else Transform()

        tr = use_el.get("transform")
        if tr:
            try:
                t_transform = Transform(tr)
                return t_transform @ t_translate
            except Exception as e:
                self.log(logging.WARNING, f"Cannot apply transform {tr} to node {self.node_str(use_el)}")
                self.log(logging.WARNING, str(e))
                self.log(logging.WARNING, traceback.format_exc())
                pass

        return t_translate

    def append_transform(self, el:BaseElement, t:Transform) -> None:
        """
        Append a transform to an element's existing transform.

        This helper safely merges a new transform `t` into the element's current
        `transform` attribute. It only operates on real geometry nodes
        (`inkex.ShapeElement`). The existing transform is parsed; if parsing
        fails, a warning is logged and the base transform defaults to identity.

        The final transform is computed as:

            combined = t · base

        and written back to the element as a string. If `t` is None or the
        element is not a shape, the function performs no action.

        Parameters
        ----------
        el : BaseElement
            The SVG element whose transform should be updated.
        t : Transform
            The transform to append.

        Returns
        -------
        None
            The element's `transform` attribute is updated in place.
        """

        if t is None:
            return
            
        if not isinstance(el, inkex.ShapeElement):
            return
            
        try:
            base = Transform(el.get("transform") or "")
        except Exception as e:
            self.log(logging.WARNING, f"Cannot append transform {t} to {self.node_str(el)}")
            self.log(logging.WARNING, str(e))
            self.log(logging.WARNING, traceback.format_exc())

            base = Transform()
            
        combined = t @ base
        el.set("transform", str(combined))


    def transform_path(self, node:PathElement, transform:Transform) -> BaseElement:
        """
        Apply a geometric transform to a <path> element, preserving subpaths and
        updating stroke width.

        This helper converts the element's path data to absolute coordinates,
        applies the provided transform matrix, and writes the transformed path
        back to the node. All subpaths are preserved exactly as Inkex's
        `to_absolute()` produces them. After transforming the geometry, the
        stroke width is adjusted via `scale_stroke_width()` so that visual
        appearance remains consistent under scaling transforms.

        Parameters
        ----------
        node : BaseElement
            The <path> element whose geometry should be transformed.
        transform : Transform
            The affine transform to apply.

        Returns
        -------
        BaseElement
            The same node, with updated path data and stroke width.
        """

        self.log(logging.DEBUG, f"Transform {self.node_str(node)}, transform={transform}")

        # Parse existing path, preserving all subpaths
        p = node.path.to_absolute()

        # Apply CTM safely
        p = p.transform(transform)

        # Write back
        node.path = p

        self.scale_stroke_width(node, transform)

        return node

            
    def transform_circle(self, node:Circle, transform:Transform) -> PathElement | BaseElement:
        """
        Transform a <circle> element, preserving circular geometry when possible.

        The transform matrix is decomposed into scale, rotation, and skew
        components. If the transform introduces **non-uniform scaling** or
        **skew**, the circle can no longer remain a true circle; in that case the
        element is converted to a <path> via `circle_to_path()` and the full
        transform is applied.

        If the transform consists only of **uniform scale + rotation +
        translation**, the circle is preserved as a circle:

        - The center `(cx, cy)` is transformed by the affine matrix.
        - The radius is scaled uniformly by `scaleX` (which equals `scaleY` in
        this case).

        This preserves the semantic meaning of a circle whenever mathematically
        valid, and falls back to path conversion only when required.

        Parameters
        ----------
        node : BaseElement
            The <circle> element to transform.
        transform : Transform
            The affine transform to apply.

        Returns
        -------
        PathElement | BaseElement
            The updated <circle> element when uniform scaling is possible,
            otherwise a new <path> element representing the transformed circle.
        """

        self.log(logging.DEBUG, f"Transform {self.node_str(node)}, transform={transform}")

        cx = float(node.get("cx") or 0)
        cy = float(node.get("cy") or 0)
        r  = float(node.get("r") or 0)

        (a, c, e), (b, d, f) = transform.matrix

        # --- Compute scale and skew ---
        scaleX = math.sqrt(a*a + b*b)
        scaleY = math.sqrt(c*c + d*d)
        skew   = a*c + b*d

        # --- Case 1: Non-uniform scale or skew → convert to path ---
        if abs(scaleX - scaleY) > 1e-9 or abs(skew) > 1e-9:
            return self.circle_to_path(node, transform)

        # --- Case 2: Uniform scale + rotation + translation → preserve circle ---
        # Transform center
        cx2 = a*cx + c*cy + e
        cy2 = b*cx + d*cy + f

        # Scale radius
        r2 = r * scaleX

        node.set("cx", str(cx2))
        node.set("cy", str(cy2))
        node.set("r",  str(r2))

        return node

    
    def transform_rect(self, node:Rectangle, transform:Transform) -> BaseElement | PathElement:
        """
        Transform a <rect> element while preserving true rectangular geometry
        whenever possible.

        The affine transform matrix is inspected to determine whether the rectangle
        can remain axis-aligned or must be converted into a path. Depending on the
        matrix components, the rectangle may be translated, uniformly scaled, or
        converted into a path when the transform introduces rotation, skew, or
        non-uniform scaling.

        Three transform categories are handled:

        1. Pure translation  
        Occurs when the transform matrix represents only a shift in x and y,
        without any scaling, rotation, or skew. In this case, the rectangle is
        moved by the translation offsets while its width and height remain
        unchanged.

        2. Uniform scale (with optional translation)  
        Occurs when the transform matrix applies the same scale factor in both
        x and y directions, possibly combined with translation. The rectangle is
        scaled uniformly, meaning both width and height are multiplied by the
        same factor, and the position is transformed accordingly.

        3. General transform  
        Any transform that introduces non-uniform scaling, rotation, or skew
        prevents the rectangle from remaining axis-aligned. In this case, the
        element is converted into a path via `rect_to_path()`, and the full
        transform is applied to that path.

        Stroke width is scaled before geometry handling to preserve visual
        appearance.

        Args:
            node (BaseElement): The <rect> element to transform.
            transform (Transform): The affine transform to apply.

        Returns:
            BaseElement | PathElement: The updated <rect> if its geometry can be
            preserved, otherwise a new <path> element representing the transformed
            rectangle.
        """

        self.log(logging.DEBUG, f"Transform {self.node_str(node)}, transform={transform}")

        self.scale_stroke_width(node, transform)

        x = float(node.get("x") or 0)
        y = float(node.get("y") or 0)
        w = float(node.get("width") or 0)
        h = float(node.get("height") or 0)

        (a, c, e), (b, d, f) = transform.matrix

        # Pure translation
        if a == 1 and d == 1 and b == 0 and c == 0:
            node.set("x", str(x + e))
            node.set("y", str(y + f))
            return node

        # Uniform scale (with or without translation)
        if b == 0 and c == 0 and a == d:
            node.set("x", str(x * a + e))
            node.set("y", str(y * a + f))
            node.set("width",  str(w * abs(a)))
            node.set("height", str(h * abs(a)))
            return node

        # Anything else → becomes a path
        return self.rect_to_path(node, transform=transform)


    def transform_ellipse(self, node:Ellipse, transform:Transform) -> BaseElement | PathElement:
        """
        Transform an <ellipse> element while preserving true elliptical geometry
        whenever possible.

        The affine transform matrix is inspected to determine whether the ellipse
        can remain axis-aligned or must be converted into a path. Depending on the
        matrix components, the ellipse may be translated, uniformly scaled, or
        converted into a path when the transform introduces rotation, skew, or
        non-uniform scaling.

        Three transform categories are handled:

        1. Pure translation  
        Occurs when the transform matrix represents only a shift in x and y,
        without any scaling, rotation, or skew. In this case, the ellipse is
        moved by the translation offsets while its radii remain unchanged.

        2. Uniform scale (with optional translation)  
        Occurs when the transform matrix applies the same scale factor in both
        x and y directions, possibly combined with translation. The ellipse is
        scaled uniformly, meaning both radii are multiplied by the same factor,
        and the center is transformed accordingly.

        3. General transform  
        Any transform that introduces non-uniform scaling, rotation, or skew
        prevents the ellipse from remaining axis-aligned. In this case, the
        element is converted into a path via `ellipse_to_path()`, and the full
        transform is applied to that path.

        Stroke width is scaled before geometry handling to preserve visual
        appearance.

        Args:
            node (Ellipse): The <ellipse> element to transform.
            transform (Transform): The affine transform to apply.

        Returns:
            BaseElement | PathElement: The updated <ellipse> if its geometry can be
            preserved, otherwise a new <path> element representing the transformed
            ellipse.
        """

        self.log(logging.DEBUG, f"Transform {self.node_str(node)}, transform={transform}")

        self.scale_stroke_width(node, transform)

        cx = float(node.get("cx") or 0)
        cy = float(node.get("cy") or 0)
        rx = float(node.get("rx") or 0)
        ry = float(node.get("ry") or 0)

        (a, c, e), (b, d, f) = transform.matrix

        # Pure translation
        if a == 1 and d == 1 and b == 0 and c == 0:
            node.set("cx", str(cx + e))
            node.set("cy", str(cy + f))
            return node

        # Uniform scale (with or without translation)
        if b == 0 and c == 0 and a == d:
            node.set("cx", str(cx * a + e))
            node.set("cy", str(cy * a + f))
            node.set("rx", str(rx * abs(a)))
            node.set("ry", str(ry * abs(a)))
            return node

        # Anything else → becomes a path
        return self.ellipse_to_path(node, transform=transform)

    def is_identity(self, t:Transform) -> bool:
        """
        Return True if the transform is the identity matrix.

        Inkex's `Transform.matrix` flattens the 2x3 affine matrix into a
        6-tuple `(a, b, c, d, e, f)` representing:

            [ a  c  e ]
            [ b  d  f ]
 
        The identity transform is therefore:

            (1, 0, 0, 1, 0, 0)

        This helper performs a direct tuple comparison to check whether the
        transform applies no scaling, rotation, skew, or translation.

        Parameters
        ----------
        t : Transform
            The transform to test.

        Returns
        -------
        bool
            True if `t` is the identity transform, otherwise False.
        """

        return t.matrix == (1, 0, 0, 1, 0, 0)

    # endregion

    # region --- Styles ---

    def parse_css_rules(self) -> dict[str, dict[str, str]]:
        """
        Parse all CSS rules contained in <style> elements in the SVG.

        Each <style> node may contain one or more CSS rule blocks. The text
        content of the element is passed to `parse_css_block()`, which extracts
        individual selectors and their property dictionaries. All rules from all
        <style> elements are merged into a single dictionary, with later rules
        overwriting earlier ones if selectors collide.

        Returns
        -------
        dict[str, dict[str, str]]
            A mapping of CSS selectors to their parsed property dictionaries.
        """

        rules = {}

        # Find all <style> elements
        for style_elem in self.svg.xpath("//svg:style", namespaces=inkex.NSS):
            css = style_elem.text or ""
            block_rules = self.parse_css_block(css)
            rules.update(block_rules)

        return rules


    def parse_css_block(self, css: str) -> dict[str, dict[str, str]]:
        """
        Parse a single CSS block into selector → property mappings.

        The input string may contain multiple CSS rule blocks. Each match from
        `CSS_RULE` yields a selector list and a declaration body. Selectors are
        split on commas (e.g. `.a, .b`) and each selector is assigned the same
        property dictionary.

        Declarations inside the body are split on semicolons. Only entries
        containing a colon are kept, producing a mapping:

            { property_name : property_value }

        All selectors found in the block are added to the result dictionary. If
        the block contains no valid matches, no rules are produced.

        Returns
        -------
        dict[str, dict[str, str]]
            A mapping of CSS selectors to dictionaries of parsed properties.
        """

        rules = {}

        for match in self.CSS_RULE.finditer(css):
            # Safety guard
            if not match:
                self.log(logging.WARNING,f"Ignoring CSS entry {css}")
                continue

            selector_text = match.group("selectors").strip()
            body = match.group("body").strip()

            # Multiple selectors: ".a, .b"
            selectors = [s.strip() for s in selector_text.split(",")]

            props = {}
            for decl in body.split(";"):
                decl = decl.strip()
                if ":" not in decl:
                    continue
                prop, val = decl.split(":", 1)
                props[prop.strip()] = val.strip()

            for sel in selectors:
                rules[sel] = props

        return rules


    def classify_css_rules(self, rules: dict[str, dict[str, str]] ) -> (
            
        tuple[
            dict[str, dict[str, str]],
            dict[str, dict[str, str]],
            dict[str, dict[str, str]],
            ]
    ):
        
        """
        Classify parsed CSS rules into class, element, and ID selectors.

        The input dictionary maps raw selectors (e.g. ".foo", "#bar", "rect")
        to their property dictionaries. This function separates them into three
        groups:

        - Class selectors:    ".name"   → stored without the leading dot
        - ID selectors:       "#name"   → stored without the leading hash
        - Element selectors:  "rect", "path", "circle", etc.

        Each selector is assigned the property dictionary it was parsed with.
        The three resulting dictionaries are returned as a tuple in the order:

            (class_rules, element_rules, id_rules)

        Returns
        -------
        tuple[dict[str, dict[str, str]], dict[str, dict[str, str]], dict[str, dict[str, str]]]
            The classified selector dictionaries.
        """

        class_rules = {}
        element_rules = {}
        id_rules = {}

        for selector, props in rules.items():
            if selector.startswith("."):
                class_rules[selector[1:]] = props
            elif selector.startswith("#"):
                id_rules[selector[1:]] = props
            else:
                # element selector: rect, path, circle, etc.
                element_rules[selector] = props

        self.log(logging.DEBUG, f"Class rules = {class_rules}")
        self.log(logging.DEBUG, f"Element rules = {element_rules}")
        self.log(logging.DEBUG, f"ID rules = {id_rules}")

        return class_rules, element_rules, id_rules


    def resolve_css_classes(self):
        """
        Resolve CSS selectors (element, class, ID) into presentation attributes.

        This function applies parsed CSS rules to all SVG elements. It handles
        three selector types in cascade order:

        1. Element selectors (e.g. `rect`, `path`, `circle`)
        If the element's tag matches an element selector, all properties that
        correspond to presentation attributes are written directly onto the
        element.

        2. Class selectors (e.g. `.foo`)
        If the element has a `class` attribute, each class name is checked
        against the parsed class rules. Matching properties are applied, and
        the `class` attribute is removed afterward.

        3. ID selectors (e.g. `#logo`)
        If the element has an `id` attribute matching an ID rule, the rule's
        properties are applied.

        Only properties listed in `PRESENTATION_ATTRS` are written to the
        element; all others are ignored. The function counts how many properties
        were applied and logs a summary when finished.

        Returns
        -------
        None
            Presentation attributes are applied directly to elements in-place.
        """

        raw_rules = self.parse_css_rules()
        class_rules, element_rules, id_rules = self.classify_css_rules(raw_rules)

        count = 0

        for elem in self.svg.iter():
            tag = self.tag_name(elem)

            # 1. Element selectors
            if tag in element_rules:
                self.log(logging.DEBUG, f"Matched element selector -> {element_rules[tag]}")
                for prop, value in element_rules[tag].items():
                    if prop in self.PRESENTATION_ATTRS:
                        elem.set(prop, value)
                        count += 1

            # 2. Class selectors
            cls = elem.get("class")
            if cls:
                for class_name in cls.split():
                    if class_name in class_rules:
                        self.log(logging.DEBUG, f"Matched class selector -> {class_rules[class_name]}")

                        for prop, value in class_rules[class_name].items():
                            if prop in self.PRESENTATION_ATTRS:
                                elem.set(prop, value)
                                count += 1
                del elem.attrib["class"]

            # 3. ID selectors
            elem_id = elem.get("id")
            if elem_id and elem_id in id_rules:
                self.log(logging.DEBUG, f"Matched ID selector -> {id_rules[elem_id]}")

                for prop, value in id_rules[elem_id].items():
                    if prop in self.PRESENTATION_ATTRS:
                        elem.set(prop, value)
                        count += 1

        if count:
            self.log(logging.INFO, f"Resolved {count} CSS class/element/id styles")


    def resolve_styles_to_attributes(self, node:BaseElement|None = None) -> None:
        """
        Resolve inline `style="..."` attributes into presentation attributes.

        This function walks the SVG tree (starting from `node` or the document
        root) and converts every inline CSS declaration into explicit XML
        attributes. For each element with a `style` attribute:

        - The style string is parsed via `Style(...)`.
        - Each property is checked against `PRESENTATION_ATTRS`.
        - Matching properties are written directly onto the element.
        - The original `style` attribute is removed.

        This is part of the CSS-flattening pipeline, ensuring that all styling
        is expressed as presentation attributes before further geometry or
        transform processing. The function counts how many elements were
        modified and logs a summary.

        Parameters
        ----------
        node : BaseElement | None
            Optional subtree root. If None, the entire SVG document is processed.

        Returns
        -------
        None
            Elements are updated in place.
        """

        count = 0

        if node is None:
            node = self.svg

        # svg is the root element (SvgDocumentElement)
        for elem in node.iter():
            style_attr = elem.get("style")
            if not style_attr:
                continue

            style = Style(style_attr)

            # CSS overrides presentation attributes
            for prop, value in style.items():
                if prop in self.PRESENTATION_ATTRS:
                    elem.set(prop, str(value))

            del elem.attrib["style"]
            count += 1

        if count:
            self.log(logging.INFO, f"Resolved {count} styles to attributes")

    # endregion
            
    # region --- Resolve References ---

    def remap_ids_in_clone(self, clone:BaseElement) -> None:
        """
        Remap IDs inside a cloned geometry subtree and update all internal URL-based
        references accordingly.

        This function is used after expanding <use>, gradients, masks, or any
        operation that duplicates geometry. The cloned subtree must not retain
        the original IDs, otherwise collisions and cross-document reference
        leaks occur. The procedure is two-phase:

        1. **Assign new IDs**
        Using `iter_geometry_subtree()`, every geometry element in the clone
        is visited. If an element has an `id`, a fresh unique ID is generated
        via `generate_id()`. A mapping:

            old_to_new = { old_id → new_id }

        is built for later reference rewriting.

        2. **Rewrite internal references**
        Every element in the clone is scanned for attributes that may contain
        URL references:

            clip-path, mask, fill, stroke, filter

        For each attribute, `ref_target()` is used to extract the referenced
        node and its ID. If the referenced ID appears in `old_to_new`, the
        attribute is rewritten using `node_or_id_to_url(new_id)` so that the
        clone becomes self-contained and consistent.

        This ensures that cloned geometry has unique IDs and that all internal
        links point to the correct remapped targets.

        Parameters
        ----------
        clone : BaseElement
            The root of the cloned subtree whose IDs and references should be
            remapped.

        Returns
        -------
        None
            The clone is modified in place.
        """

        old_to_new = {}

        # 1. Assign new IDs
        for el, _ in self.iter_geometry_subtree(clone, attr=""):
            old_id = el.get("id")
            if old_id:
                new_id = self.generate_id(el)
                old_to_new[old_id] = new_id

        # 2. Rewrite references inside the clone
        for el in clone.iter():
            for attr in ("clip-path", "mask", "fill", "stroke", "filter"):
                val = el.get(attr)
                if not val:
                    continue

                ref, ref_id = self.ref_target(el, attr)
                if not ref is None and ref_id in old_to_new:
                    el.set(attr, self.node_or_id_to_url(old_to_new[ref_id]))


    def expand_all_uses(self, node:BaseElement|None=None, visited:set|None=None) -> None:
        """
        Deterministically expand all <use> elements via post-order traversal.

        This is the core <use>-expansion routine. It guarantees that every
        expandable <use> is replaced by a fully flattened clone of its referenced
        subtree, wrapped in a group that carries the <use> element's transform.
        The algorithm is deliberately **post-order**:

            children → node

        so that when a <use> is expanded, its referenced subtree has already been
        flattened and contains **no remaining <use> elements**.

        Processing steps
        ----------------
        1. **Traverse children first**
        Ensures referenced geometry is already flattened before cloning.

        2. **Check if node is <use>**
        Non-<use> nodes are ignored at this stage.

        3. **Resolve reference**
        `ref_target()` returns the referenced element and its ID.
        - If the reference is missing or not expandable, the <use> is removed.
        - If the reference ID is already in `visited`, a cycle is detected and the <use> is removed.

        4. **Clone referenced subtree**
        A deep copy is made. All IDs inside the clone are remapped via
        `remap_ids_in_clone()` so the clone becomes self-contained.

        5. **Inherit presentation attributes**
        Attributes from the <use> override those of the referenced element
        only when `override=False` allows it.

        6. **Inherit clip-path**
        If the <use> has a clip-path, it is copied to the clone.

        7. **Strip <use>-specific attributes**
        Removes `href`, `xlink:href`, `x`, and `y`.

        8. **Insert wrapper group**
        A fresh `<g>` is inserted at the <use>'s position. Its transform is the
        full <use> transform (`use_transform()`), and its own ID is removed to
        avoid collisions.

        9. **Attach clone**
        The clone is inserted as a child of the wrapper group.

        10. **Remove original <use>**
            The <use> element is deleted from the DOM.

        Guarantees
        ----------
        - Expanded clones contain **no <use> elements**.
        - Cycles are detected and removed safely.
        - ID collisions are prevented through remapping.
        - Only one deterministic traversal is required.

        Parameters
        ----------
        node : BaseElement | None
            The subtree root to process. Defaults to the SVG root.
        visited : set | None
            Tracks visited reference IDs to detect cycles.

        Returns
        -------
        None
            The SVG tree is modified in place.
        """

        if node is None:
            node = self.svg
        if visited is None:
            visited = set()

        # --- 1. Process children first (post-order) ---
        for child in list(node):
            self.expand_all_uses(child, visited)

        # --- 2. Process this node ---
        if self.tag_name(node) != "use":
            return

        # Resolve reference
        ref, ref_id = self.ref_target(node)
        if ref is None or not self.is_expandable_ref(ref):
            # Remove useless <use>
            parent, _ = self.parent_of(node)
            self.remove_node(node, parent)
            return

        # Cycle detection
        if ref_id and ref_id in visited:
            # Remove cyclic <use>
            parent, _ = self.parent_of(node)
            self.remove_node(node, parent)
            return

        if ref_id:
            visited = set(visited)
            visited.add(ref_id)

        # --- Clone referenced element ---
        clone = copy.deepcopy(ref)
        self.remap_ids_in_clone(clone)

        # Inherit presentation attributes
        self.copy_presentation_attributes(node, clone, override=False)

        # Inherit clip-path
        clip_attr = node.get("clip-path")
        if clip_attr:
            clone.set("clip-path", clip_attr)

        # Strip <use>-specific attributes
        for attr in ("href", f"{{{self.XLINK_NS}}}href", "x", "y"):
            clone.attrib.pop(attr, None)

        # IMPORTANT:
        # Because we are in post-order traversal,
        # the referenced subtree has already been flattened.
        # Therefore clone contains NO <use> elements.

        # --- Insert wrapper group with transform ---
        parent, idx = self.parent_of(node)
        wrapper = inkex.Group()
        wrapper.attrib.pop("id", None)

        self.add_node(wrapper, parent, idx)
        self.append_transform(wrapper, self.use_transform(node))

        # Attach clone
        self.add_node(clone, wrapper)

        self.log_svg(clone, header=f"Expanded  {self.node_str(wrapper)}")

        # Remove original <use>
        self.remove_node(node, parent)

    # region --- Marker ---

    def unit(self, z:complex) -> complex:
        """
        Normalize a complex vector. Complex numbers are used to represent (x,y) coordinates.

        This helper returns the unit-length direction of a complex number `z`,
        interpreted as a 2D vector `(x, y)`. The magnitude is computed using
        `math.hypot()`, which is numerically stable for very small or very large
        values.

        If the vector has zero length, the function returns `0j` to avoid
        division by zero.

        Parameters
        ----------
        z : complex
            The vector to normalize.

        Returns
        -------
        complex
            A complex number of magnitude 1 pointing in the same direction as
            `z`, or `0j` if `z` has zero length.
        """

        x, y = z.real, z.imag
        l = math.hypot(x, y)
        return complex(x / l, y / l) if l else 0j


    def compute_tangent(self, cmd:"PathCmd", first:complex, prev:complex, prev_prev:complex=0j, t:float=0.0) -> complex:
        """
        Compute the unit tangent vector for a path command at parameter `t`.

        This function attempts to use the command's own derivative-based tangent
        (`cmd.unit_tangent()`), which is the mathematically correct method for
        all curve types (Cubic, Quadratic, Arc, etc.). If the command does not
        implement `unit_tangent()` or raises an exception, a robust fallback is
        used:

            tangent = unit( end_point(cmd, first) - prev )

        This fallback provides a reasonable direction for straight-line segments
        and ensures marker orientation and path sampling never fail.

        Parameters
        ----------
        cmd : PathCmd
            The Inkex path command whose tangent should be evaluated.
        first : complex
            The start point of the current subpath (needed for Z commands).
        prev : complex
            The previous endpoint before this command.
        prev_prev : complex, optional
            The endpoint before `prev`, used by some tangent implementations.
        t : float, optional
            Parameter along the command (0 ≤ t ≤ 1). Defaults to 0.0.

        Returns
        -------
        complex
            A unit-length tangent vector. Returns `0j` if the tangent cannot be
            computed.
        """

        try:
            # Inkscape command objects provide a derivative-based tangent
            v = cmd.unit_tangent(first, prev, prev_prev, t)
            return complex(v.x, v.y)

        except Exception:
            # Fallback: use the direction from prev → end
            p0 = prev
            p1 = self.end_point(cmd, first)
            if p0 is None or p1 is None:
                return 0j
            return self.unit(p1 - p0)


    def angle_from_vec(self, v:complex)-> float:
        """
        Convert a 2D vector into a normalized angle in degrees.

        The vector `v` is interpreted as `(x, y)` using its real and imaginary
        parts. The angle is computed using `atan2(y, x)`, which yields the
        correct orientation in all quadrants, including negative axes and
        degenerate cases. The result is converted to degrees and wrapped into
        the canonical symmetric interval:

            [-180°, 180°]

        Zero-length vectors return `0.0` to avoid undefined orientation.

        Parameters
        ----------
        v : complex
            The 2D vector whose angle should be computed.

        Returns
        -------
        float
            The normalized angle in degrees, guaranteed to lie within
            [-180, 180].
        """

        x, y = v.real, v.imag
        if x == 0 and y == 0:
            return 0.0

        a = math.degrees(math.atan2(y, x))
        return (a + 180.0) % 360.0 - 180.0


    def iter_vertices(self, el:BaseElement) -> Iterator[tuple[str, float, float, float]]:
        """
        Dispatch vertex-iteration based on element type.

        This is the unified entry point for extracting marker-relevant vertices
        from any geometry element. The function inspects the element's SVG tag
        and forwards to the appropriate specialized iterator:

        - circle → ``iter_circle_vertices()``
        - ellipse → ``iter_ellipse_vertices()``
        - path / rect → ``iter_path_vertices()``
        - other elements → treated as paths after normalization

        All iterators yield tuples of the form::

            (vtype, x, y, angle)

        where:
        - ``vtype`` ∈ {"start", "mid", "end"}
        - ``x, y`` are vertex coordinates
        - ``angle`` is the marker orientation in degrees

        This abstraction ensures that marker placement logic can treat all SVG
        geometry uniformly.

        Parameters
        ----------
        el : BaseElement
            The SVG element whose vertices should be iterated.

        Returns
        -------
        Iterator[tuple[str, float, float, float]]
            A generator yielding marker vertices for the element.
        """

        tag = self.tag_name(el)

        match(tag):
            case "circle":
                yield from self.iter_circle_vertices(el)
            case "ellipse":
                yield from self.iter_ellipse_vertices(el)
            case "path", "rect":
                yield from self.iter_path_vertices(el)
            case _:
                yield from self.iter_path_vertices(el)


    def iter_circle_vertices(self, circle:BaseElement) -> Iterator[tuple[str, float, float, float]]:
        """
        Generate Inkscape-accurate marker vertices for a <circle> element.

        Inkscape does not treat circles as a single parametric curve when
        placing markers. Instead, it internally converts a <circle> into a
        four-segment arc path. Marker placement follows the arc endpoints:

            - Start marker  → first arc start
            - Mid markers   → arc endpoints for segments 0, 1, 2
            - End marker    → last arc end (same coordinate as start)

        This yields five vertices total, with the start and end sharing the
        same coordinate but having different tangent orientations.

        Vertex layout (counter-clockwise):
            - rightmost  → start / end
            - bottom     → mid
            - leftmost   → mid
            - top        → mid

        Angles follow Inkscape's tangent conventions:
            - Start:  +90°
            - Bottom: 180°
            - Left:   270°
            - Top:    0°
            - End:    -90° (same point as start, opposite tangent)

        This function reproduces Inkscape's exact marker behavior, ensuring
        transform-consistent marker placement.

        Parameters
        ----------
        circle : BaseElement
            The <circle> element whose marker vertices should be enumerated.

        Returns
        -------
        Iterator[tuple[str, float, float, float]]
            Yields (type, x, y, angle) for each marker vertex.
        """

        cx = float(circle.get("cx") or 0)
        cy = float(circle.get("cy") or 0)
        r  = float(circle.get("r") or 0) 

        # Inkscape-style circle marker vertices:
        # Four arc endpoints + start/end at same coordinate.

        # Start (rightmost)
        yield ("start", cx + r, cy, 90.0)

        # Mid 1 (bottom)
        yield ("mid", cx, cy + r, 180.0)

        # Mid 2 (leftmost)
        yield ("mid", cx - r, cy, 270.0)

        # Mid 3 (top)
        yield ("mid", cx, cy - r, 0.0)

        # End (same coordinate as start, different tangent)
        yield ("end", cx + r, cy, -90.0)
        

    def iter_ellipse_vertices(self, ellipse:BaseElement) -> Iterator[tuple[str, float, float, float]]:
        """
        Generate Inkscape-accurate marker vertices for an <ellipse> element.

        Inkscape treats an <ellipse> the same way it treats a <circle>: as a
        four-segment arc path with marker vertices placed at the arc endpoints.
        Unlike circles, ellipses have non-uniform radii, so their tangent angles
        cannot be taken from fixed compass directions. Tangents must be computed
        from the analytic derivative of the ellipse.

        Parametric definition:
            x(t) = cx + rx * cos(t)
            y(t) = cy + ry * sin(t)

        Derivative (tangent vector):
            dx/dt = -rx * sin(t)
            dy/dt =  ry * cos(t)

        The tangent angle is:
            atan2(dy/dt, dx/dt)

        This matches Inkscape's marker orientation exactly.

        Vertex layout:
        Markers are placed at the four canonical parametric angles:
            - rightmost  → t = 0°
            - bottom     → t = 90°
            - leftmost   → t = 180°
            - top        → t = 270°

        As with circles, the start and end markers share the same coordinate but
        have different tangents:
            - Start uses t = 0°
            - End uses t = 360° (2π), producing the opposite tangent

        This yields five vertices total: one start, three midpoints, and one end.

        Args:
            ellipse (BaseElement): The <ellipse> element whose marker vertices
                should be enumerated.

        Returns:
            Iterator[tuple[str, float, float, float]]: Yields (type, x, y, angle)
            for each marker vertex, matching Inkscape's marker placement behavior.
        """

        cx = float(ellipse.get("cx") or 0)
        cy = float(ellipse.get("cy") or 0)
        rx = float(ellipse.get("rx") or 0)
        ry = float(ellipse.get("ry") or 0)

        # Parametric angles for the 4 arc endpoints
        # t = 0°   → rightmost
        # t = 90°  → bottom
        # t = 180° → leftmost
        # t = 270° → top
        ts = [0.0, math.pi/2, math.pi, 3*math.pi/2]

        # Compute tangent angle for each t
        def tangent_angle(t):
            vx = -rx * math.sin(t)
            vy =  ry * math.cos(t)
            return math.degrees(math.atan2(vy, vx))

        # Start (t = 0)
        t0 = ts[0]
        yield ("start",
            cx + rx,
            cy,
            tangent_angle(t0))

        # Mid markers (t = 90°, 180°, 270°)
        for t in ts[1:]:
            x = cx + rx * math.cos(t)
            y = cy + ry * math.sin(t)
            yield ("mid", x, y, tangent_angle(t))

        # End (same coordinate as start, different tangent)
        # Inkscape uses t = 360° for end marker
        yield ("end",
            cx + rx,
            cy,
            tangent_angle(2 * math.pi))


    def iter_path_vertices(self, el:BaseElement) -> Iterator[tuple[str, float, float, float]]:
        """
        Iterate all marker-relevant vertices of a normalized SVG path.

        This routine reproduces **Inkscape's exact marker placement logic** for
        paths, including correct tangent computation at segment boundaries,
        handling of closed vs. open subpaths, and proper start/mid/end marker
        orientation.

        Workflow
        --------
        For each subpath produced by ``iter_subpaths()``:

        1. **Start vertex**

        - Located at the subpath's first point (``sub_start``)
        - Tangent is taken from the first command at ``t = 0``
        - Orientation: ``angle_from_vec(unit(tangent))``

        2. **Mid vertices**

        For every segment boundary where both the previous and current command
        have valid endpoints:

        - Compute incoming tangent from the previous command at ``t = 1``
        - Compute outgoing tangent from the current command at ``t = 0``
        - Normalize both, sum them, and normalize again
        - If the sum is zero (perfect cusp), fall back to outgoing tangent
        - Yield a ``"mid"`` vertex at the previous segment's endpoint

        This matches Inkscape's “bisector tangent” rule for marker-mid
        orientation.

        3. **End vertex**

        - For open paths: placed at the final endpoint
        - For closed paths: placed at the *start* point, but using the final
            segment's tangent at ``t = 1``
        - Orientation: ``angle_from_vec(unit(tangent))``

        Closed-path behavior
        --------------------
        Closed paths (``Z`` or ``z``) do **not** place the end marker at the last
        point. Instead, Inkscape places the end marker at the start point with
        the tangent of the closing segment. This function mirrors that behavior
        exactly.

        Output format
        -------------
        Each yielded vertex is a tuple::

            (vtype, x, y, angle)

        where:

        - ``vtype`` ∈ {"start", "mid", "end"}
        - ``x, y`` are coordinates (floats)
        - ``angle`` is the marker orientation in degrees

        Parameters
        ----------
        el : BaseElement
            The SVG element whose path vertices should be enumerated.

        Returns
        -------
        Iterator[tuple[str, float, float, float]]
            Yields all marker vertices for the element's path geometry.
        """

        path = self.normalize_path(el.path)

        for sub_start, cmds in self.iter_subpaths(path):

            closed = any(c.letter.upper() == "Z" for c in cmds)

            # --- first vertex: marker-start -----------------------------------------
            first_cmd = cmds[0]
            out_vec = self.compute_tangent(first_cmd, sub_start, sub_start, 0j, t=0.0)
            angle_start = self.angle_from_vec(self.unit(out_vec))
            yield ("start", sub_start.real, sub_start.imag, angle_start)

            # --- mid vertices: marker-mid -------------------------------------------
            prev_cmd = None
            prev_start = sub_start
            prev_end = sub_start

            for cmd in cmds:
                curr_end = self.end_point(cmd, sub_start)
                if curr_end is None:
                    prev_cmd = cmd
                    continue

                if prev_cmd is not None:
                    in_vec = self.compute_tangent(prev_cmd, sub_start, prev_start, 0j, t=1.0)
                    out_vec = self.compute_tangent(cmd, sub_start, prev_end, 0j, t=0.0)

                    in_vec = self.unit(in_vec)
                    out_vec = self.unit(out_vec)

                    v = in_vec + out_vec
                    if v == 0j:
                        v = out_vec

                    angle_mid = self.angle_from_vec(v)
                    yield ("mid", prev_end.real, prev_end.imag, angle_mid)

                prev_cmd = cmd
                prev_start = prev_end
                prev_end = curr_end

            # --- last vertex: marker-end --------------------------------------------
            if prev_cmd is not None and prev_end is not None:
                in_vec = self.compute_tangent(prev_cmd, sub_start, prev_start, 0j, t=1.0)
                angle_end = self.angle_from_vec(self.unit(in_vec))

                if closed:
                    # closed path: end marker at start point, with last segment's tangent
                    yield ("end", sub_start.real, sub_start.imag, angle_end)
                else:
                    # open path: end marker at last point
                    yield ("end", prev_end.real, prev_end.imag, angle_end)


    def compute_marker_viewbox_transform(self, marker:BaseElement) -> Transform:
        """
        Compute the marker's viewBox → markerWidth/markerHeight transform.

        This reproduces the transform Inkscape applies when converting marker
        geometry from its internal coordinate system (the viewBox) into the
        final marker-sized coordinate system used during rendering.

        Two cases exist:

        1. **Marker has a viewBox**
        The marker's geometry is defined in an arbitrary coordinate system
        `[vb_x, vb_y, vb_w, vb_h]`. To map this into the marker's actual
        display size (`markerWidth`, `markerHeight`), SVG requires:

            Scale = (markerWidth / vb_w, markerHeight / vb_h)
            Translate = (-refX - vb_x, -refY - vb_y)

        The order matters. Inkex composes transforms *in reverse*, so to
        achieve:

            Matrix = Scale @ Translate

        you must call `add_scale()` **before** `add_translate()`.

        This yields a transform that:
        - Moves the viewBox origin to the marker's reference point
        - Scales geometry into the markerWidth/markerHeight box

        2. **Marker has no viewBox**
        SVG defines that marker geometry is already in markerWidth/markerHeight
        units. Only the reference point translation is needed:

            Translate = (-refX, -refY)

        Parameters
        ----------
        marker : BaseElement
            The <marker> element whose viewBox transform should be computed.

        Returns
        -------
        Transform
            The transform mapping marker geometry into markerWidth/markerHeight
            space, with correct reference-point alignment.
        """

        vb = marker.get("viewBox")
        refX = float(marker.get("refX") or 0)
        refY = float(marker.get("refY") or 0)

        if vb:
            vb_x, vb_y, vb_w, vb_h = map(float, vb.split())
            mw = float(marker.get("markerWidth") or 3)  # SVG defaults markerWidth to 3 if omitted
            mh = float(marker.get("markerHeight") or 3) # SVG defaults markerHeight to 3 if omitted

            sx = mw / vb_w
            sy = mh / vb_h


            # To achieve Matrix = Scale @ Translate, we must call scale FIRST in inkex chaining
            tr = inkex.Transform()
            tr.add_scale(sx, sy)
            tr.add_translate(-refX - vb_x, -refY - vb_y)
            return tr
        else:
            # No viewBox means local coordinates match markerWidth/markerHeight system directly
            tr = inkex.Transform()
            tr.add_translate(-refX, -refY)
            return tr


    def marker_style(self, el:BaseElement) -> Style:
        """
        Return the element's marker-style as a fully parsed Style object.

        When splitting shapes into a filled shape and an outline to reflect SVG 2.0
        paint-order, one of the shapes may receive a `marker-style` attribute so
        that context-dependent fill, stroke, and stroke-width inheritance can be
        resolved correctly for marker geometry.

        This helper guarantees that downstream marker logic always receives a
        valid Style instance, regardless of whether the element explicitly defines
        a `marker-style` attribute. It unifies two cases:

        1. Explicit marker-style  
        If the element carries a `marker-style="..."` attribute, it is parsed
        directly into a Style object.

        2. Synthesized fallback  
        If no marker-style is present, a synthetic style string is constructed
        from the element's presentation attributes:
            - stroke  
            - stroke-width  
            - fill  
            - role (defaults to "stroke")

        This ensures that marker resolution always has access to stroke color,
        stroke width, fill color, and role. The returned Style object is never
        None, simplifying downstream code and eliminating repeated fallback logic
        in marker placement, tangent computation, and marker geometry transforms.

        Args:
            el (BaseElement): The SVG element whose marker-style should be resolved.

        Returns:
            Style: A fully populated Style object representing the marker's styling
            context.
        """


        # --- 1. Parse once ---
        style_attr = el.get("marker-style")
        if style_attr:
            style = Style(style_attr)
        else:
            # fallback: synthesize a Style object from presentation attributes
            style = Style(
                f"stroke:{el.get('stroke', '#000')};"
                f"stroke-width:{el.get('stroke-width', '1.0')};"
                f"fill:{el.get('fill', 'none')};"
                f"role:stroke;"
            )

        self.log(logging.DEBUG, f"marker-style={style}")

        return style


    def compute_vertex_transform(self, style:Style, marker:BaseElement, x:float, y:float, angle:float, pos:str) -> Transform:
        """
        Compute the per-vertex transform for placing a marker instance.

        This transform is the *instance-level* part of marker placement: it
        positions, rotates, and scales the marker geometry at a specific vertex
        (x, y) with a specific tangent angle. It does **not** include the
        viewBox→markerWidth/markerHeight transform; that is handled separately by
        `compute_marker_viewbox_transform()`.

        Transform components
        --------------------
        1. **Translation**
        The marker is positioned at the vertex coordinates:

            (x, y)

        2. **Rotation**
        Determined by the marker's `orient` attribute:

        - `"auto"`  
            Use the vertex tangent angle.

        - `"auto-start-reverse"`  
            Reverse the angle by 180° only for the `"start"` vertex.

        - Numeric value  
            Interpret `orient="45"` etc. as an absolute rotation in degrees.

        3. **Scaling**
        Controlled by `markerUnits`:

        - `"strokeWidth"`  
            Scale the marker by the element's stroke width.  
            This matches SVG's rule that markers scale with stroke thickness.

        - `"userSpaceOnUse"`  
            No per-instance scaling; marker geometry stays in user units.

        The scale is uniform:

            sx = sy = stroke_width   (strokeWidth mode)
            sx = sy = 1.0            (userSpaceOnUse)

        Composition order
        -----------------
        Inkex composes transforms in reverse order, so calling:

            tr.add_translate()
            tr.add_rotate()
            tr.add_scale()

        produces:

            Matrix = Translate @ Rotate @ Scale

        which is the correct SVG marker instance transform.

        Parameters
        ----------
        style : Style
            The resolved marker-style for the element, providing stroke-width.
        marker : BaseElement
            The <marker> element being instantiated.
        x, y : float
            Vertex coordinates.
        angle : float
            Tangent angle at the vertex, in degrees.
        pos : str
            One of {"start", "mid", "end"}.

        Returns
        -------
        Transform
            The per-vertex marker transform (translation + rotation + scaling).
        """

        orient = marker.get("orient", "auto")
        units  = marker.get("markerUnits", "strokeWidth")

        # --- Rotation ---
        if orient == "auto":
            rot = angle
        elif orient == "auto-start-reverse":
            rot = angle + 180 if pos == "start" else angle
        else:
            # orient given as absolute degrees (number)
            rot = float(orient or 0)

        # --- Scaling (per-instance only) ---
        stroke_width = float(style.get("stroke-width") or 1.0)

        if units == "strokeWidth":
            sx = stroke_width
            sy = stroke_width
        else:
            sx = 1.0
            sy = 1.0

        # --- Compose transform ---
        tr = inkex.Transform()
        tr.add_translate(x, y)
        tr.add_rotate(rot)
        tr.add_scale(sx, sy)

        return tr


    def expand_marker_instance(self, el:BaseElement, style:Style, marker:BaseElement, vertex:tuple[float,float,float], pos:str, insert_pos:int) -> int:
        """
        Expand a single marker instance at a given vertex and insert its geometry
        into the SVG DOM at the correct position.

        This function performs the full per-vertex marker expansion pipeline,
        matching Inkscape's rendering model while producing GT7-safe geometry.
        It is called once per marker vertex and returns the updated insertion
        index so multiple marker geometries can be placed in the correct DOM
        order.

        Pipeline:
            1. Marker viewBox normalization  
            The marker's internal coordinate system is converted into
            markerWidth/markerHeight space using the viewBox transform and
            refX/refY alignment. This produces `viewbox_tr`.

            2. Per-instance placement  
            The marker is positioned, rotated, and scaled according to:
                - vertex coordinates (x, y)  
                - tangent angle  
                - orient="auto", "auto-start-reverse", or numeric  
                - markerUnits="strokeWidth" or "userSpaceOnUse"  
            This produces `vertex_tr`.

            3. Shape-level transform  
            The element's own transform (including inherited transforms) is
            applied so markers follow the geometry exactly.

            4. Clone marker geometry  
            The <marker> element is resolved into a geometry subtree. Each
            geometry node is transformed individually.

            5. Final transform composition  
            For each geometry node:
                final_tr = shape_tr @ vertex_tr @ viewbox_tr @ geom_tr  
            This matches Inkscape's transform order and ensures correct
            placement under nested groups, transforms, and markerUnits scaling.

            6. Stroke-width correction  
            Because vertex transforms may include strokeWidth scaling, the
            geometry's stroke-width is divided by the effective scale so the
            rendered stroke matches the original element's stroke thickness.

            7. Insertion into DOM  
            The transformed geometry is inserted at `insert_pos`, which is then
            incremented. The caller controls the initial insertion index based
            on paint-order and marker role.

        Return value:
            The updated insertion index, allowing the caller to place multiple
            marker geometries sequentially without losing ordering.

        Args:
            el (BaseElement): The element whose marker is being expanded.
            style (Style): The resolved marker-style for the element.
            marker (BaseElement): The <marker> definition being instantiated.
            vertex (tuple[float, float, float]): The (x, y, angle) for this marker
                vertex.
            pos (str): One of {"start", "mid", "end"}.
            insert_pos (int): DOM insertion index for the first geometry node.

        Returns:
            int: The next insertion index after all geometry nodes have been added.
        """

        x, y, angle = vertex

        # 1. Marker coordinate-system normalization
        viewbox_tr = self.compute_marker_viewbox_transform(marker)
        self.log(logging.DEBUG, f"viewbox_tr={viewbox_tr}")

        # 2. Per-instance placement (strokeWidth scaling, orient, refX/refY)
        vertex_tr = self.compute_vertex_transform(style, marker, x, y, angle, pos)
        self.log(logging.DEBUG, f"vertex_tr={vertex_tr}")

        parent, idx = self.parent_of(el)
        shape_tr = self.local_transform(el)
        self.log(logging.DEBUG, f"shape_tr={shape_tr}")

        # Clone marker geometry
        clone = self.resolve_marker_geometry(el, style, marker)

        self.log(logging.DEBUG, f"marker={pos}, parent_index={idx}, insert_pos={insert_pos}")

        for geom, _ in self.iter_geometry_subtree(clone, attr="", only_gt7_geometry=False):
            geom_tr = Transform(geom.get("transform"))
            self.log(logging.DEBUG, f"geom_tr={geom_tr}")

            # --- APPLY PER-VERTEX PLACEMENT ---
            final_tr = shape_tr @ vertex_tr @ viewbox_tr @ geom_tr
            self.log(logging.DEBUG, f"final_tr={final_tr}")

            _, geom = self.apply_transform_to_node(geom, final_tr)
            geom.attrib.pop("transform", None)

            # --- CORRECT STROKE-WIDTH (remove vertex_tr scaling) ---
            scale = self.stroke_scale(el, vertex_tr @ viewbox_tr)
            if scale > 0:
                raw_sw = geom.get("stroke-width")
                try:
                    sw = float(raw_sw) if raw_sw not in (None, "", "none") else 1.0
                except ValueError:
                    self.log(logging.DEBUG, f"Cannot parse stroke-width='{raw_sw}'")
                    sw = 1.0

                geom.set("stroke-width", str(sw / scale))

            self.log(logging.DEBUG, f"[MARKER] {pos} --> {self.node_str(geom)} at index {insert_pos}")

            self.add_node(geom, parent, insert_pos)
            insert_pos += 1

        return insert_pos
    

    def resolve_parent_attributes(self, el:BaseElement, style:Style, marker:BaseElement) -> None:
        """
        Resolve context-dependent fill/stroke and stroke-width inheritance inside a
        <marker> clone, using the parent element's resolved style.

        This function applies the *parent element's* styling context to the marker's
        geometry, reproducing Inkscape's marker-painting rules. It is called after
        the marker has already been cloned and all references resolved, but before
        per-vertex transforms are applied.

        What this function does
        -----------------------

        1. **Remove IDs**
        Every geometry node inside the marker clone has its `id` removed so that
        new, collision-free IDs can be assigned later when inserting into the DOM.

        2. **Resolve context-dependent fill**
        SVG allows marker geometry to use:
        - `fill="context-stroke"` → use the parent element's stroke color
        - `fill="context-fill"`   → use the parent element's fill color
        - `fill="none"` or missing → no fill

        This ensures marker shapes visually match the parent element's styling.

        3. **Resolve context-dependent stroke**
        Similarly:
        - `stroke="context-stroke"` → parent stroke color
        - `stroke="context-fill"`   → parent fill color
        - `stroke="none"` or missing → no stroke

        4. **Normalize stroke-width (markerUnits="strokeWidth")**
        When `markerUnits="strokeWidth"`, SVG requires marker geometry to scale
        with the parent element's stroke width. Inkscape implements this by:

        - If the marker geometry has **no** stroke-width:
            inherit the parent's stroke-width directly.
        - If the marker geometry **does** have a stroke-width:
            multiply it by the parent's stroke-width.

        This matches Inkscape's behavior and ensures consistent marker thickness.

        5. **No scaling for markerUnits="userSpaceOnUse"**
        In this mode, marker geometry stays in user units and stroke-width is
        *not* inherited or scaled.

        Why fill sometimes becomes the stroke color
        -------------------------------------------
        Because SVG defines:

        - `fill="context-stroke"` → “use the stroke color of the referencing element”

        This is intentional. It allows arrowheads and other markers to match the
        stroke color of the path they are attached to. For example, a black path
        with `fill="none"` and `stroke="red"` will produce a red arrowhead even if
        the marker geometry originally had `fill="context-stroke"`.

        This is not a bug — it is the correct interpretation of the SVG spec and
        matches Inkscape's rendering.

        Parameters
        ----------
        el : BaseElement
            The element that references the marker (e.g., a <path>).
        style : Style
            The resolved marker-style for the parent element.
        marker : BaseElement
            The cloned marker whose geometry should be styled.

        Returns
        -------
        None
            The marker clone is modified in place.
        """

        self.log_svg(marker, header="BEFORE STYLING")

        units = marker.get("markerUnits", "strokeWidth")

        for geom, _ in self.iter_geometry_subtree(marker, attr="", only_gt7_geometry=False):
            # remove IDs --> generates default ID later when adding to DOM
            geom.attrib.pop("id", None)
            
            # Resolve fill
            fill = geom.get("fill")
            if fill == "context-stroke":
                geom.set("fill", style.get("stroke"))
            elif fill == "context-fill":
                geom.set("fill", style.get("fill"))
            elif fill in (None, "none"):
                geom.set("fill", "none")

            # Resolve stroke
            stroke = geom.get("stroke")

            if stroke == "context-stroke":
                geom.set("stroke", style.get("stroke"))

            elif stroke == "context-fill":
                geom.set("stroke", style.get("fill"))

            elif stroke in (None, "none"):
                geom.set("stroke", "none")

            # Normalize stroke width for start/end markers
            if units == "strokeWidth":
                sw_attr = geom.get("stroke-width")
                
                if sw_attr is None:
                    geom.set("stroke-width", style.get("stroke-width", "1.0"))
                else:
                    sw = self.to_float(sw_attr)
                    parent_sw_attr = el.get("stroke-width", "1.0")
                    parent_sw = self.to_float(parent_sw_attr)
                    geom.set("stroke-width", parent_sw * sw)


        self.log_svg(marker, header="AFTER STYLING")


    def resolve_marker_geometry(self, el:BaseElement, style:Style, marker:BaseElement) -> BaseElement:
        """
        Resolve all context-dependent styling inside a cloned <marker> and return the
        fully flattened, reference-free geometry subtree.

        This is the *final* marker-geometry preparation step before per-vertex
        placement. It produces a clone that:

            - has no ID collisions
            - has no unresolved references (<use>, gradients, clipPaths, filters, CSS)
            - has correct context-dependent fill/stroke
            - has correct stroke-width inheritance
            - has a flattened DOM (no groups with transforms)
            - is ready for per-vertex transforms and DOM insertion

        Pipeline
        --------

        1. **Clone the marker**
        A deep copy is made so the original <marker> remains untouched. The
        clone's `id` is removed to avoid collisions when inserted into <defs> or
        expanded into the DOM.

        2. **Resolve geometry**
        `resolve_geometry()` normalizes shapes, expands <use> inside the marker,
        and ensures all geometry is explicit.

        3. **Resolve references**
        `resolve_references()` rewrites gradients, clipPaths, masks, filters, and
        CSS references so the clone becomes self-contained.

        4. **Resolve parent-dependent styling**
        `resolve_parent_attributes(el, style, clone)` applies the parent element's
        styling context:
        - fill="context-stroke" → parent stroke color  
        - fill="context-fill"   → parent fill color  
        - stroke="context-stroke" → parent stroke color  
        - stroke="context-fill"   → parent fill color  
        - stroke-width inheritance for markerUnits="strokeWidth"

        This matches Inkscape's marker-painting rules exactly.

        5. **Flatten DOM**
        `flatten_svg_dom(clone)` removes nested transforms by pushing them down
        into geometry nodes, producing a clean, transform-free subtree.

        6. **Return the resolved clone**
        The caller will apply per-vertex transforms and insert the geometry into
        the DOM.

        Why this matters
        ----------------
        Markers are one of the most complex parts of SVG rendering. They combine:

        - independent coordinate systems (viewBox)
        - per-instance transforms (orient, strokeWidth scaling)
        - parent-dependent styling (context-stroke/fill)
        - geometry flattening
        - reference resolution
        - ID remapping

        This function ensures that *before* any per-vertex placement occurs, the
        marker geometry is already in a fully resolved, predictable state.

        Parameters
        ----------
        el : BaseElement
            The element that references the marker (e.g., a <path>).
        style : Style
            The resolved marker-style for the parent element.
        marker : BaseElement
            The <marker> definition to clone and resolve.

        Returns
        -------
        BaseElement
            A fully resolved, flattened, self-contained marker clone ready for
            per-vertex placement.
        """

        self.log(logging.DEBUG, f"Resolving geometry of {self.node_str(marker)}")

        # Clone marker
        clone = copy.deepcopy(marker)
        clone.attrib.pop("id", None)

        # Fully resolve references inside the clone
        self.resolve_geometry(clone)
        self.resolve_references(clone)
        self.resolve_parent_attributes(el, style, clone)
        self.flatten_svg_dom(clone)

        self.log_svg(clone, header="RESOLVED MARKER")

        return clone


    def resolve_markers_for_element(self, el:BaseElement) -> int:
        """
        Resolve and expand all markers (start, mid, end) for a geometry element.

        This is the top-level marker-expansion routine. It orchestrates the entire
        marker pipeline:

            1. Parse paint-order.
            2. Resolve marker references once.
            3. Remove marker attributes from the element.
            4. Resolve marker-style.
            5. Determine DOM insertion position.
            6. Iterate geometry vertices.
            7. Expand marker instances at each vertex.

        The function returns the total number of marker instances inserted.

        High-level behavior:

            SVG markers are painted either below or above the element depending on
            `paint-order`. The relevant paint-order positions are:

                - fill
                - stroke
                - marker

            The element's `marker-start`, `marker-mid`, and `marker-end` attributes
            are resolved once, then removed from the element to prevent double
            expansion and ensure deterministic behavior.

        Marker-style:

            The element may define a `marker-style="..."` attribute or rely on a
            synthesized fallback derived from stroke, fill, stroke-width, and role.
            The resolved style determines:

                - stroke color
                - fill color
                - stroke-width
                - role ("stroke" or "fill")

            The role determines whether markers should be inserted relative to the
            stroke or fill paint-order position.

        Insertion position:

            If `marker_pos < comp_pos`, markers are inserted below the element:

                insert_pos = idx

            Otherwise, markers are inserted above the element:

                insert_pos = idx + 1

            This matches Inkscape's marker layering rules.

        Vertex iteration:

            Vertices are obtained from:

                - iter_circle_vertices
                - iter_ellipse_vertices
                - iter_path_vertices

            Each vertex yields:

                (vtype, x, y, angle)

            where vtype ∈ {"start", "mid", "end"}.

            For each vertex, the corresponding marker is expanded using
            expand_marker_instance, which performs:

                - viewBox normalization
                - per-vertex placement
                - strokeWidth scaling
                - stroke-width correction
                - geometry insertion

        Return value:

            The total number of marker instances inserted.

        Args:
            el (BaseElement): The geometry element whose markers should be resolved
                and expanded.

        Returns:
            int: Number of marker instances inserted into the DOM.
        """

        self.log(logging.DEBUG, f"Resolving marker for node {self.node_str(el)}")

        fill_pos, stroke_pos, marker_pos = self.parse_paint_order(el)

        # no marker defined
        if marker_pos < 0:
            return 0

        marker_types = ["start", "mid", "end"]

        # Resolve markers once and remove marker attributes
        markers = { }

        for marker_type in marker_types:
            attr = "marker-" + marker_type

            marker, _ = self.ref_target(el, attr)
            markers[marker_type] = marker

            el.attrib.pop(attr, None)

        style = self.marker_style(el)
        el.attrib.pop("marker-style", None)

        # Parse paint-order and adjust insert_pos
        _, idx = self.parent_of(el)

        if style.get("role", "stroke") == "stroke":
            comp_pos = stroke_pos
        else:
            comp_pos = fill_pos

        # insert markers below or above parent shape

        if marker_pos < comp_pos:
            insert_pos = idx
        else:
            insert_pos = idx + 1

        count = 0

        # Single pass over vertices
        for vtype, x, y, angle in self.iter_vertices(el):

            marker = markers.get(vtype)
            if marker is None:
                continue

            insert_pos = self.expand_marker_instance(el, style, marker, (x, y, angle), vtype, insert_pos)
            count += 1

        return count

    # endregion

    # region --- Resolve References ---

    def remove_filter_for_element(self, el:BaseElement) -> int:
        """
        Remove a filter reference from an element and report how many were removed.

        This helper performs the minimal, SVG-correct cleanup for filter usage on a
        geometry element. It only handles **direct** filter references:

            filter="url(#someFilter)"

        and does **not** attempt to remove the corresponding <filter> node from
        <defs>. That responsibility belongs to higher-level cleanup passes such as:

        - cleanup_defs
        - remove_unreferenced_referenceable

        Those routines operate on the global referenced-ID set and will remove the
        actual <filter> element once it is no longer referenced.

        Behavior
        --------
        1. Check whether the element has a `filter` attribute.
        2. If present:
        - Remove the attribute.
        - Log the removal.
        - Return `1`.
        3. If absent:
        - Return `0`.

        This keeps the function deterministic and side-effect-free: it only modifies
        the element itself, not the <defs> tree.

        Parameters
        ----------
        el : BaseElement
            The element whose filter reference should be removed.

        Returns
        -------
        int
            Number of filter references removed (0 or 1).
        """

        # Check direct filter attribute
        filter_ref = el.get("filter")
        if filter_ref:
            el.attrib.pop("filter", None)
            self.log(logging.WARNING, f"Removed filter from <{self.node_str(el)}>")
            return 1

        return 0
    
    def remove_mask_for_element(self, el:BaseElement) -> int:
        """
        Remove a mask reference from an element and report how many were removed.

        This helper mirrors the behavior of `remove_filter_for_element()` and keeps
        mask cleanup deliberately minimal and local:

            - It only removes the *attribute* `mask="url(#...)"` from the element.
            - It does **not** delete the corresponding <mask> node from <defs>.
            Global cleanup is handled later by:
                - cleanup_defs
                - remove_unreferenced_referenceable

        Those passes operate on the full referenced-ID set and will remove the
        <mask> element once it is no longer referenced anywhere.

        Behavior
        --------
        1. Check whether the element has a `mask` attribute.
        2. If present:
        - Remove the attribute.
        - Log the removal.
        - Return `1`.
        3. If absent:
        - Return `0`.

        This keeps the function deterministic and avoids premature deletion of
        <mask> nodes that may still be referenced elsewhere.

        Parameters
        ----------
        el : BaseElement
            The element whose mask reference should be removed.

        Returns
        -------
        int
            Number of mask references removed (0 or 1).
        """

        # Check direct mask attribute
        mask_ref = el.get("mask")
        if mask_ref:
            el.attrib.pop("mask", None)
            self.log(logging.WARNING, f"Removed mask from {self.node_str(el)}")
            return 1

        return 0

    def replace_unsupported_shape(self, node:BaseElement) -> Tuple[BaseElement, int]:
        """
        Replace unsupported SVG shape elements (<rect> with rounded corners,
        <polyline>, <polygon>, <line>) with equivalent <path> geometry.

        This function performs a *single-node* replacement and returns:

            (new_node, count)

        where:
        - **new_node** is either the original node or the replacement <path>
        - **count** is 1 if a replacement occurred, otherwise 0

        It does *not* recursively traverse children. That is handled by
        replace_unsupported_shapes.

        Supported replacements
        ----------------------
        1. **Rounded <rect> → <path>**
        If rx or ry is non-zero, the rectangle is converted into a path with
        explicit arc commands. This matches Inkscape's internal conversion and
        ensures GT7 compatibility.

        2. **<polyline> / <polygon> → <path>**
        Converted into a path with straight line segments. Polygons are closed.

        3. **<line> → <path>**
        Converted into a simple “M x1,y1 L x2,y2” path.

        Transform handling
        ------------------
        The node's *local* transform is passed into the conversion helpers
        (`rect_to_path`, `poly_to_path`, `line_to_path`). These helpers produce
        a geometry node with the transform already applied, so the replacement
        node is transform-free and ready for insertion.

        DOM replacement
        ---------------
        If a replacement occurs:

        1. The parent and index of the original node are retrieved.
        2. The original node is removed.
        3. The new node is inserted at the same index.
        4. A log entry is emitted.

        If the node has already been replaced earlier (e.g., by clipping logic),
        the parent may be None; in that case, no DOM replacement is attempted.

        Return value
        ------------
        (new_node, count)

        Parameters
        ----------
        node : BaseElement
            The SVG element to inspect and possibly replace.

        Returns
        -------
        Tuple[BaseElement, int]
            The replacement node and the number of replacements performed.
        """

        count = 0
        new_node = node

        tag = self.tag_name(node)
        
        # Skip paint servers only
        match tag:
    
            # Rounded rect → path
            case "rect":
                rx = float(node.get("rx", "0") or "0")
                ry = float(node.get("ry", "0") or "0")
                if rx != 0 or ry != 0:
                    new_node = self.rect_to_path(node, self.local_transform(node), replace_node=False)
                    count += 1

            # polyline → path
            case "polyline" | "polygon":
                new_node = self.poly_to_path(node, self.local_transform(node), replace_node=False)
                count += 1

            # line → path
            case "line":
                new_node = self.line_to_path(node, self.local_transform(node),  replace_node=False)
                count += 1

            case _:
                pass

        if count > 0:
            parent, idx = self.parent_of(node)

            # Check if node got replaced already, e.g. by clipping
            if not parent is None:
                self.remove_node(node, parent=parent)
                self.add_node(new_node, parent, idx)

                self.log(logging.INFO, f"Replaced {self.node_str(new_node)} with {self.node_str(new_node)}")

        return new_node, count


    def resolve_references(self, node:BaseElement|None=None) -> None:
        """
        Resolve all reference-bearing attributes and paint-server dependencies in the
        SVG tree, using a **postorder traversal** (children first, then the node
        itself). This is the central reference-normalization pass for the entire
        document.

        It ensures that by the time geometry flattening occurs, no unresolved paint servers, 
        filters, masks, clipPaths, patterns, markers, or gradients remain on geometry nodes.

        Traversal model
        ---------------
        The function performs a **postorder** walk:

            - Recurse into children (except paint-server containers)
            - Process the node itself

        Paint-server containers are skipped during recursion:

        - <defs>
        - <styles>
        - <clipPath>
        - <pattern>
        - <mask>
        - <filter>
        - <linearGradient>
        - <radialGradient>
        - <meshGradient>

        These nodes are handled by dedicated normalization passes elsewhere.

        Per-node processing
        -------------------
        Depending on the tag, different resolution steps apply:

        ### 1. Groups (<g>)
        - resolve_gradient_for_group
        - resolve_pattern_for_group
        - resolve_clippath_for_group

        Groups may carry presentation attributes that need to be pushed down or
        resolved.

        ### 2. Geometry nodes
        Tags:
        - path
        - rect
        - circle
        - ellipse
        - line
        - polyline
        - polygon

        For these, the following are resolved:

        - Gradients  
        `grad_count += resolve_gradient_for_shape(node)`

        - Clip paths  
        `clip_count += resolve_clippath_for_shape(node)`

        - Filters  
        `filter_count += remove_filter_for_element(node)`

        - Masks  
        `mask_count += remove_mask_for_element(node)`

        - Patterns  
        `pattern_count += resolve_pattern_for_shape(node)`

        - Markers  
        `marker_count += resolve_markers_for_element(node)`

        This ensures that all geometry nodes are free of unresolved references.

        ### 3. Other nodes
        For all other tags, only filters and masks are removed:

        - `filter_count += remove_filter_for_element(node)`
        - `mask_count += remove_mask_for_element(node)`

        This avoids accidental processing of paint-server definitions.

        Root-level reporting
        --------------------
        After the entire tree has been processed, the root node (`self.svg`)
        emits summary logs:

        - Normalized gradients
        - Resolved clip paths
        - Removed filters
        - Removed masks
        - Resolved patterns
        - Resolved markers
        - Resolved paint-order attributes

        This provides a clear diagnostic overview of what the normalization pass
        accomplished.

        Why postorder?
        --------------
        Postorder guarantees that:

        - Child geometry is fully normalized before parent-level attributes are
        resolved.
        - Group-level presentation attributes do not interfere with unresolved
        references inside children.
        - Marker expansion later sees a fully normalized tree.

        Parameters
        ----------
        node : BaseElement | None
            The node to process. If None, the root SVG element is used.

        Returns
        -------
        None
            The SVG tree is modified in place.
        """

        grad_count = 0
        clip_count = 0
        filter_count = 0
        mask_count = 0
        pattern_count = 0
        marker_count = 0
        paint_order_count = 0

        if node is None:
            node = self.svg

        # Postorder traversal: recurse children first, then the node itself

        for child in list(node):
            tag = self.tag_name(child).lower()

            if tag in ("defs", "styles", "clippath", "pattern", "mask", "filter", "lineargradient", "radialgradient", "meshgradient"): 
                continue

            self.resolve_references(child)

        # Process node itself after children have been processed

        tag = self.tag_name(node).lower()

        match tag:

            case "g":
                self.resolve_gradient_for_group(node)
                self.resolve_pattern_for_group(node)
                self.resolve_clippath_for_group(node)

            case "path" | "rect" | "circle" | "ellipse" | "line" | "polyline" | "polygon":
                grad_count += self.resolve_gradient_for_shape(node)
                clip_count += self.resolve_clippath_for_shape(node)
                filter_count += self.remove_filter_for_element(node)
                mask_count += self.remove_mask_for_element(node)
                pattern_count += self.resolve_pattern_for_shape(node)
                marker_count += self.resolve_markers_for_element(node)

            case "clippath":
                pass

            case _:
                filter_count += self.remove_filter_for_element(node)
                mask_count += self.remove_mask_for_element(node)

        if node == self.svg:
            
            if grad_count:
                self.log(logging.INFO, f"Normalized {grad_count} gradients")

            if clip_count:
                self.log(logging.INFO, f"Resolved {clip_count} clip paths")

            if filter_count:
                self.log(logging.INFO, f"Removed {filter_count} filters")

            if mask_count:
                self.log(logging.INFO, f"Removed {mask_count} masks")

            if pattern_count:
                self.log(logging.INFO, f"Resolved {pattern_count} patterns")
            
            if marker_count:
                self.log(logging.INFO, f"Resolved {marker_count} markers")

            if paint_order_count:
                self.log(logging.INFO, f"Resolved {paint_order_count} attributes")
    

    def flatten_svg_dom(self, node:BaseElement|None=None, parent_transform:Transform|None=None) -> int:
        """
        Flatten all transforms in the SVG DOM by pushing cumulative transforms
        (CTMs) down into geometry nodes and groups entirely.

        This is the core transform-normalization pass. It ensures that after
        execution, the SVG tree contains **no remaining transforms** except those
        that are explicitly part of geometry conversion (e.g., path commands
        already incorporating the transform). This is essential for GT7-safe
        geometry, marker expansion, and predictable downstream processing.

        Overview
        --------
        The function performs a **postorder traversal**:

            - Recurse into children first
            - Then process the node itself

        This guarantees that child geometry receives the correct cumulative
        transform before parent-level transforms are removed.

        Transform accumulation
        ----------------------
        Each node may carry a `transform="..."` attribute. The cumulative
        transform matrix (CTM) is computed as:

            CTM = parent_transform @ local_transform

        where:

        - `parent_transform` is the CTM of the parent
        - `local_transform` is the node's own transform (or identity)

        This CTM is passed down to children so they inherit all transforms from
        their ancestors.

        Node-specific behavior
        ----------------------

        ### 1. Geometry nodes
        Tags:
        - path
        - rect
        - circle
        - ellipse
        - line
        - polyline
        - polygon

        These nodes receive the full CTM via:

            count, node = apply_transform_to_node(node, CTM)

        This rewrites the geometry so that the transform is *baked into* the
        shape itself. The node's `transform` attribute is removed.

        ### 2. Groups (<g>)
        Groups may contain transforms but do not carry geometry. Their transforms
        are absorbed into the CTM passed to children. After children are
        flattened, the group itself is normalized via:

            flatten_group(node)

        which removes presentation attributes and ensures no transform remains.

        ### 3. Paint-server containers
        Tags:
        - defs
        - styles
        - clipPath
        - pattern
        - mask
        - filter
        - linearGradient
        - radialGradient
        - meshGradient

        These nodes are **skipped** during recursion. Their transforms must not be
        flattened because they define coordinate systems for paint servers.

        ### 4. Special nodes
        Tags:
        - clippath
        - marker
        - pattern

        These nodes are left untouched at the node level. Their children may be
        processed depending on tag rules.

        Root-level reporting
        --------------------
        When `node == self.svg`, the function logs:

            "Resolved N transformations"

        where N is the total number of geometry nodes whose transforms were
        flattened.

        Why postorder?
        --------------
        Postorder traversal ensures:

        - Child geometry receives the correct CTM before parent transforms are removed.
        - No transform is lost or applied twice.
        - Flattening is stable even under nested groups, nested transforms, and
        complex SVG structures.

        Parameters
        ----------
        node : BaseElement | None
            The node to process. If None, the root SVG element is used.
        parent_transform : Transform | None
            The cumulative transform matrix from ancestors.

        Returns
        -------
        int
            Number of geometry nodes whose transforms were flattened.
"""

        if node is None:
            node = self.svg

        if parent_transform is None:
            parent_transform = Transform()

        transform_count = 0

        # Parse local transform safely
        tstr = node.get("transform", None)
        local_transform = Transform(tstr) if tstr else Transform()

        # Combine CTMs

        CTM = parent_transform @ local_transform

        # Postorder traversal: recurse children first, then the node itself

        for child in list(node):
            tag = self.tag_name(child)
            if tag in ("defs", "styles", "clipPath", "pattern", "mask", "filter", "linearGradient", "radialGradient", "meshgradient"): 
                continue
            
            transform_count += self.flatten_svg_dom(child, CTM)

        # Process node itself after children have been processed

        self.log(logging.DEBUG, f"Flattening {self.node_str(node)}")

        tag = self.tag_name(node)

        match tag:
    
            case "g":
                self.flatten_group(node)

            case "path" | "rect" | "circle" | "ellipse" | "line" | "polyline" | "polygon":
                count, node = self.apply_transform_to_node(node, CTM)
                transform_count += count

            case "clippath", "marker", "pattern":
                pass

        if node == self.svg:
            self.log(logging.INFO, f"Resolved {transform_count} transformations")

        return transform_count

    # endregion
            

    # region --- Viewbox Translation ---

    def viewbox(self) -> tuple[float, float, float, float]:
        """
        Return the SVG viewBox as a 4-tuple (vx, vy, vw, vh). This function is a
        pure accessor: it reads the viewBox attribute exactly as written on the
        root <svg> element and performs no geometry fallback or bounding-box
        computation.

        If the element defines a viewBox of the form “vx vy vw vh”, the four
        numbers are parsed and returned as floats. If no viewBox is present, the
        function returns (0, 0, 0, 0) to signal that no explicit viewBox exists.

        This design keeps the accessor predictable and inexpensive. Any fallback
        logic—such as computing a geometry-based bounding box or normalizing
        coordinates—is handled by higher-level routines.

        Returns:
            tuple[float, float, float, float]:
                The parsed (vx, vy, vw, vh) values, or (0, 0, 0, 0) if no viewBox
                attribute is defined.
        """

        vb = self.svg.get("viewBox")
        if vb is None:
        # fallback: use geometry bbox
            return 0, 0, 0, 0

        vx, vy, vw, vh = map(float, vb.split())
        return (vx, vy, vw, vh)


    def compute_viewbox_translation(self, v_x:float, v_y:float) -> Transform:
        """
        Compute the pure viewBox-origin translation for an SVG document.

        This helper applies the minimal transform required to shift the SVG
        coordinate system so that the viewBox origin `(v_x, v_y)` becomes `(0, 0)`.

        It intentionally performs **no scaling** and **no width/height adjustment**.
        Its sole purpose is to remove the viewBox offset, which is often needed
        before flattening transforms, normalizing geometry, or exporting to systems
        that expect a zero-origin coordinate space (e.g., GT7 decal workflows).

        Mathematical definition
        -----------------------
        Given a viewBox:

            viewBox="v_x v_y v_w v_h"

        the translation required to move the origin to `(0, 0)` is:

            tx = -v_x
            ty = -v_y

        The resulting transform is:

            translate(tx, ty)

        This is equivalent to:

        - shifting all geometry left by `v_x`
        - shifting all geometry up by `v_y`

        Usage context
        -------------
        This function is typically used when:

        - The SVG root has a non-zero viewBox origin.
        - You want to normalize coordinates before flattening transforms.
        - You want to ensure consistent placement of geometry in downstream pipelines.
        - You need a predictable coordinate system for marker expansion, clipping, or GT7-safe export.

        It does **not** modify the DOM. It only returns a `Transform` object.

        Parameters
        ----------
        v_x : float
            The viewBox's x-origin.
        v_y : float
            The viewBox's y-origin.

        Returns
        -------
        Transform
            A translation transform that shifts the viewBox origin to (0, 0).
        """

        tx = -v_x
        ty = -v_y

        t = Transform(f"translate({tx},{ty})")

        self.log(logging.DEBUG, f"[VBOX] Viewbox translation={t}")

        return t


    def translate_viewbox(self) -> None:
        """
        Translate the SVG's viewBox origin to (0,0) by applying a uniform translation
        to all geometry and then rewriting the viewBox accordingly.

        This is a **late-pipeline normalization step**: it assumes that all geometry
        has already been flattened, all transforms resolved, and all marker/paint-server
        references normalized. Its sole purpose is to eliminate non-zero viewBox
        origins, which are not supported by the GT7 livery editor.

        What this function does
        -----------------------

        1. **Read the current viewBox**
        Using viewbox(), retrieve:

            (v_x, v_y, v_w, v_h)

        If the viewBox is missing, this function does nothing.

        2. **Compute translation**
        compute_viewbox_translation(), compute:

            translate(-v_x, -v_y)

        This shifts all geometry so that the viewBox origin becomes (0,0).

        3. **Apply translation to all geometry**
        For every node where `is_geometry(el)` is true:

            apply_transform_to_node(el, t)

        This bakes the translation into the geometry itself, removing the need
        for a transform attribute.

        4. **Rewrite the viewBox**
        If the viewBox had valid width/height:

            viewBox="0 0 v_w v_h"

        This preserves the original dimensions but normalizes the origin.

        5. **Log completion**
        A summary log entry is emitted:

            "Translated viewbox to positive coordinates (geometry + gradients)"

        Why gradients are mentioned
        ---------------------------
        Even though gradients are not directly transformed here, earlier passes
        (e.g., resolve_references()) ensure that
        gradient coordinate systems are already normalized. This function completes
        the final geometry shift so gradients remain visually correct.

        Why this is safe late in the pipeline
        -------------------------------------
        By the time this function runs:

        - All transforms have been flattened
        - All marker geometry has been expanded
        - All clipPaths, patterns, and filters have been resolved or removed
        - No remaining transform attributes exist on geometry nodes

        This guarantees that a uniform translation is safe and will not break
        coordinate systems.

        Parameters
        ----------
        None

        Returns
        -------
        None
            The SVG DOM is modified in place.
        """

        root = self.svg

        v_x, v_y, v_w, v_h = self.viewbox()

        self.log(logging.DEBUG, f"Viewbox=(x={v_x}, y={v_y}, width={v_w}, height={v_h})")

        t = self.compute_viewbox_translation(v_x, v_y)
        if self.is_identity(t):
            return

        for el in root.iter():

            if self.is_geometry(el):
                self.log(logging.DEBUG, f"[VBOX] Translating node {self.node_str(el)}")
                _, el = self.apply_transform_to_node(el, t)

        # Rewrite viewBox to positive coordinates (if a valid viewbox was defined before)
        if v_w > 0 and v_h > 0:
            self.log(logging.DEBUG, f"[VBOX] New viewbox=(x=0, y=0, width={v_w}, height={v_h})")
            root.set("viewBox", f"0 0 {v_w} {v_h}")

        self.log(logging.INFO, "Translated viewbox to positive coordinates (geometry + gradients)")


    # endregion

    # region --- Units ---
    
    def convert_with_unit(self, num:float, unit:str) -> float:
        """
        Convert a numeric length from an arbitrary SVG unit into **px**, using the
        canonical CSS/SVG absolute-unit conversion table.

        This helper is the low-level primitive used by higher-level routines such as:

        - to_px()
        - normalize_units()
        - normalize_transform()

        It performs **no default-unit inference**, **no unitless handling**, and
        **no error recovery**. It assumes:

            - `num` is already a float
            - `unit` is one of {"px","mm","cm","in","pt","pc"}

        and returns the corresponding px value.

        Conversion table
        ----------------
        The conversion factors follow the CSS absolute-length specification:

        - **px** → 1 px  
        - **mm** → 3.779527559 px  
        - **cm** → 37.79527559 px  
        - **in** → 96 px  
        - **pt** → 96/72 px  
        - **pc** → 96/6 px  

        These constants match Inkscape's internal unit table and ensure consistent
        behavior across:

        - markerUnits="strokeWidth"
        - gradientUnits="userSpaceOnUse"
        - transform normalization
        - geometry flattening

        Logging
        -------
        If the unit is not `"px"`, a DEBUG log entry is emitted:

            [UNITS] <num> <unit> --> <value> px

        This is extremely useful when debugging unit-bearing transforms such as:

        - translate(5mm, 2mm)
        - rotate(30deg)  (deg handled elsewhere)
        - markerWidth="5mm"
        - stroke-width="0.5mm"

        Parameters
        ----------
        num : float
            The numeric value to convert.
        unit : str
            The unit suffix ("px", "mm", "cm", "in", "pt", "pc").

        Returns
        -------
        float
            The value converted into px.
        """

        value = num

        match unit:
            case "px":
                value = num
            case "mm":
                value = num * 3.779527559
            case "cm":
                value = num * 37.79527559
            case "in":
                value = num * 96.0
            case "pt":
                value = num * (96.0 / 72.0)
            case "pc":
                value = num * (96.0 / 6.0)

        if unit != "px":
            self.log(logging.DEBUG, f"[UNITS] {num} {unit} --> {value} px") 

        return value
    
    
    def to_px(self, value:str) -> float:
        r"""
        Convert a unit-bearing or unitless SVG length into px, using strict,
        spec-compliant parsing and a safe fallback path.

        This is the *canonical* entry point for unit normalization. It handles:

            - floats
            - scientific notation
            - optional whitespace
            - optional unit suffix
            - unitless values (defaulting to px)
            - invalid values (logged + safe fallback)

        It intentionally does **not** infer default units from the SVG root or
        from CSS context. That logic belongs in higher-level routines such as:

        - normalize_units()
        - normalize_transform()
        - detect_default_unit()

        Parsing model
        -------------
        The regex:

            ^([+-]?[0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)\s*(px|mm|cm|in|pt|pc)?$

        matches:

        - a valid float (with optional exponent)
        - optional whitespace
        - optional unit suffix

        Examples accepted:

        - "5"
        - "5px"
        - "12.5mm"
        - "-3.2e2cm"
        - "0.5 in"
        - "72pt"

        Unitless values
        ---------------
        If the unit suffix is missing:

            unit = "px"

        This matches SVG's rule that unitless lengths default to px *unless*
        context overrides them (e.g., markerUnits="strokeWidth", gradientUnits).

        Conversion
        ----------
        Conversion is delegated to:

        - convert_with_unit()

        which applies the canonical CSS/SVG absolute-unit table.

        Invalid values
        --------------
        If the regex fails, a WARNING is logged:

            [UNITS] Invalid numerical value <value>

        and the function returns `0.0`. This ensures robustness when encountering
        broken SVGs, CSS expansions, or Inkscape artifacts.

        Parameters
        ----------
        value : str
            The raw attribute value to parse.

        Returns
        -------
        float
            The value converted into px, or 0.0 on failure.
        """

        if value is None:
            return 0.0

        s = str(value).strip()
        if not s:
            return 0.0

        m = re.match(
            r"^([+-]?[0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)\s*(px|mm|cm|in|pt|pc)?$",
            s
        )

        if m:
            num = float(m.group(1))
            unit = m.group(2)

            # Unitless → inherit default unit
            if unit is None:
                unit = "px"

            return self.convert_with_unit(num, unit)

        self.log(logging.WARNING, f"[UNITS] Invalid numerical value {value}")

        return 0.0


    def normalize_units(self, node:BaseElement|None=None) -> BaseElement:
        """
        Normalize all unit-bearing attributes, style properties, and transform
        parameters of an SVG node (and its entire subtree) into px.

        This is the canonical, recursive unit-normalization pass. It ensures that
        every numerical attribute that may carry an SVG/CSS absolute unit
        (mm, cm, in, pt, pc) is converted into px using strict, spec-compliant
        rules. Path geometry itself is already in px and is therefore not modified.

        Pipeline
        --------
        1. Presentation attributes
        The following attributes are normalized using `to_px()`:

            x, y, cx, cy, r,
            rx, ry,
            width, height,
            stroke-width,
            markerWidth, markerHeight,
            refX, refY

        Each attribute is rewritten as a px-valued string.

        2. Transform attributes
        If the node has a `transform="..."` attribute, all unit-bearing
        parameters inside translate(), scale(), matrix(), etc. are normalized
        via:

            normalize_transform()

        This ensures that transforms no longer contain mm/cm/in/pt/pc values.

        3. Style properties
        If the node has a `style` attribute, the following style properties
        are normalized:

            stroke-width,
            marker-width, marker-height,
            x, y, width, height

        Only properties that may carry absolute units are converted.

        4. Recursive descent
        All children are processed recursively. This guarantees that the entire
        subtree becomes unit-clean before later passes such as:

        - flatten_svg_dom()
        - resolve_references()
        - expand_marker_instance()
        - translate_viewbox()

        Why this matters
        ----------------
        SVG allows absolute units in many places: presentation attributes,
        transform parameters, style properties, marker dimensions, and more.
        Downstream geometry processing (marker expansion, transform flattening,
        GT7 export) requires a uniform px-only coordinate system.

        This function provides that guarantee.

        Limitations
        -----------
        - Path geometry is not modified; it is assumed to already be in px.
        - No attempt is made to infer default units beyond the SVG rule that
        unitless values default to px.
        - No scaling or viewBox normalization is performed here; this function
        only converts units.

        Parameters
        ----------
        node : BaseElement | None
            The node to normalize. If None, the root SVG element is used.

        Returns
        -------
        BaseElement
            The same node, with all unit-bearing attributes normalized to px.
        """

        if node is None:
            node = self.svg
        
        # --- 1. Normalize presentation attributes ---
        length_attrs = [
            "x", "y", "cx", "cy", "r",
            "rx", "ry",
            "width", "height",
            "stroke-width",
            "markerWidth", "markerHeight",
            "refX", "refY",
        ]

        for attr in length_attrs:
            if attr in node.attrib:
                node.attrib[attr] = str(self.to_px(node.attrib[attr]))

        # --- 2. Normalize transform attributes ---
        if "transform" in node.attrib:
            node.attrib["transform"] = self.normalize_transform(node.attrib["transform"])

        # --- 3. Normalize style properties ---
        style = node.style
        if style:
            for key, val in list(style.items()):
                if key in ("stroke-width", "marker-width", "marker-height",
                        "x", "y", "width", "height"):
                    style[key] = str(self.to_px(val))

        # --- 4. Normalize children recursively ---
        for child in node:
            self.normalize_units(child)

        return node


    def normalize_transform(self, transform_str:str) -> str:
        r"""
        Normalize all numeric parameters inside an SVG transform string by converting
        unit-bearing and unitless values into px.

        This is the low-level transform-parameter normalizer used by
        normalize_units() and flatten_svg_dom(). It ensures that *every*
        numeric token inside a transform—whether part of translate(), scale(),
        rotate(), skew(), or matrix()—is rewritten as a px-valued float.

        Scope
        -----
        The function performs a **pure textual rewrite**:

        - It does not interpret transform semantics.
        - It does not reorder or combine transforms.
        - It does not evaluate angles (deg/rad) — those are handled elsewhere.
        - It does not apply the transform to geometry.

        Its sole job is to ensure that all numeric parameters are unit-clean.

        Parsing model
        -------------
        Every number matching:

            ([+-]?[0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)

        is replaced with:

            to_px(number)

        This means:

        - `translate(5mm, 2mm)` → `translate(18.8976, 7.55905)`
        - `translate(5, 2)`     → `translate(5.0, 2.0)` (unitless → px)
        - `matrix(1,0,0,1,10mm,5mm)` → matrix with px offsets
        - Scientific notation is supported: `1e2mm` → `377.9527559px`

        Unitless values
        ---------------
        Unitless numbers default to px, matching SVG's rule for transform
        parameters. If you need context-dependent unit inference (e.g., markerUnits),
        use normalize_transform_with_default() instead.

        Invalid values
        --------------
        If a numeric token cannot be parsed by `to_px()`, a warning is logged and
        the value becomes `0.0`. This ensures robustness when encountering malformed
        SVGs.

        Why this matters
        ----------------
        Transform attributes are one of the most common sources of hidden unit
        inconsistencies in SVG files exported from Inkscape, Illustrator, or CAD
        tools. Normalizing them early ensures:

        - predictable CTM accumulation
        - stable marker placement
        - correct geometry flattening
        - GT7-safe export

        Parameters
        ----------
        transform_str : str
            Raw transform attribute string, e.g. "translate(5mm, 2mm) rotate(30)".

        Returns
        -------
        str
            A transform string with all numeric parameters converted to px.
        """

        def repl(match):
            num = match.group(1)
            return str(self.to_px(num))

        # Replace all numbers inside transform(...) calls
        return re.sub(
            r"([+-]?[0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)",
            repl,
            transform_str
        )

    # endregion

    # region --- Geometry ---

    def rgba_to_hex(self, r: float|int, g: float|int, b: float|int, a:float|int|None=None) -> str:
        """
        Convert RGBA floats (0-1 float or 0-255 int) into a hex string.
        If alpha is omitted, output #RRGGBB.
        If alpha is provided, output #RRGGBBAA.
        """

        # Convert RGB floats in 0–1 range to 0–255
        def normalize(c):
            # Case 1: explicit integer → always 0–255
            if isinstance(c, int):
                return max(0, min(255, c))

            # Case 2: normalized float (0–1)
            if isinstance(c, float) and 0.0 <= c <= 1.0:
                return max(0, min(255, int(round(c * 255.0))))

            # Case 3: scaled float (1–255)
            if isinstance(c, float) and 1.0 < c <= 255.0:
                return max(0, min(255, int(round(c))))

            # Case 4: overshoot → clamp
            return 255

        R = normalize(r)
        G = normalize(g)
        B = normalize(b)

        hex_rgb = f"#{R:02x}{G:02x}{B:02x}"

        if a is not None:
            A = normalize(a)
            hex_rgb += f"{A:02x}"

        return hex_rgb

        

    def parse_color(self, color: str) -> tuple[str | None, int]:
        """
        Parse an SVG/CSS color string into a canonical hex RGB string and an
        integer alpha value (0-255).

        This is the high-level color parser used throughout the paint-normalization
        pipeline. It accepts any color format supported by the underlying
        ``Color`` class (named colors, hex, rgb(), rgba(), hsl(), hsla(), etc.)
        and returns a uniform representation::

            (hex_color: str | None, alpha: int)

        Behavior
        --------
        1. **Null / transparent / none**

        If the input is:

        - ``None``
        - ``"none"``
        - ``"transparent"``

        then the function returns::

            (None, 0)

        indicating “no paint”.

        2. **General color parsing**

        The function delegates parsing to::

            Color(color)

        which resolves:

        - named colors (``"red"``, ``"black"``, ``"lightgray"``)
        - hex formats (``#RGB``, ``#RGBA``, ``#RRGGBB``, ``#RRGGBBAA``)
        - functional formats (``rgb()``, ``rgba()``, ``hsl()``, ``hsla()``)
        - percentage channels
        - alpha channels

        The resolved RGB channels are converted into a canonical hex string::

            "#rrggbb"

        The alpha channel is converted into an integer 0-255.

        3. **Alpha stripping option**

        If ``self.options.strip_alpha`` is true:

        - ``alpha = 255``

        regardless of the input's alpha channel.

        4. **Fallback on parse failure**

        If ``Color(color)`` raises an exception:

        - A DEBUG log entry is emitted.
        - The function returns::

                (color, 255)

            meaning:

            - The original string is preserved as-is.
            - Alpha defaults to fully opaque.

        Return value
        ------------
        ``(hex_color, alpha)``

        Where:

        - ``hex_color`` is a canonical ``"#rrggbb"`` string or ``None``.
        - ``alpha`` is an integer in ``[0, 255]``.

        Examples
        --------
        parse_color("red")                  → ("#ff0000", 255)
        parse_color("#33669980")            → ("#336699", 128)
        parse_color("rgba(10,20,30,0.5)")   → ("#0a141e", 128)
        parse_color("none")                 → (None, 0)
        parse_color("transparent")          → (None, 0)
        parse_color("bogus")                → ("bogus", 255)

        Notes
        -----
        This function is intentionally high-level. Lower-level color normalization
        (e.g., stripping alpha from hex, converting rgba() to rgb(), or resolving
        context-dependent colors) is handled more specific functions.
        """


        if color is None:
            return None, 0

        color = color.strip().lower()

        if color == "none" or color == "transparent":
            return None, 0

        try:
            c = Color(color)
        
            hex_color = f"#{int(c.red):02x}{int(c.green):02x}{int(c.blue):02x}"
            alpha = int(c.alpha * 255) if not self.options.strip_alpha else 255

            return hex_color, alpha
            
        except Exception as e:
            self.log(logging.DEBUG, f"Failed to parse color '{color}': {e}")
            return color, 255


    def is_geometry_visible(self, el) -> bool:

        """Determine whether a geometry element produces any visible pixels.

        This method evaluates the final rendered visibility of an SVG geometry
        element after resolving inherited paint attributes, paint servers,
        opacity, and stroke/fill gating rules. It answers the question:

            “Would this element render any non-transparent pixels?”

        The function considers only paint visibility. It does not evaluate
        display/visibility properties, clipping, masking, or filter effects.

        Args:
            el: The SVG element to evaluate.

        Returns:
            bool: True if the element is visually visible, False otherwise.

        Visibility rules:
            * Non-geometry elements are treated as visible.
            * Stroke is invisible if stroke-width <= 0.
            * Fill is invisible if fill-opacity <= 0.
            * If no paint server is referenced, the direct color is parsed and
            considered visible when its alpha channel is > 0.
            * Patterns are always treated as visible.
            * Gradients are visible only if at least one stop is non-transparent.
        """

        if not self.is_geometry(el, only_gt7_supported=False):
            return True

        visible = False

        for attr in ("fill", "stroke"):
            color = (self.inherit_attribute(el, attr) or "black").strip().lower()

            if attr == "stroke":
                stroke_width = self.inherit_attribute(el, "stroke-width") or "1.0"
                if float(stroke_width) <= 0:
                    continue

            if attr == "fill":
                fill_opacity = self.inherit_attribute(el, "fill-opacity") or "1.0"
                if float(fill_opacity) <= 0:
                    continue

            # server reference
            paint_server, _ = self.ref_target(
                el, attr,
                tag_name=("linearGradient", "radialGradient", "meshgradient", "meshGradient", "pattern")
            )

            # no paint server → regular color definition
            if paint_server is None:
                # missing gradient → check color alpha
                _, alpha = self.parse_color(color)
                if alpha > 0:      # semi-transparent is visible
                    visible = True
                    break
                continue

            # pattern → assumed to be visible
            if self.tag_name(paint_server) == "pattern":
                visible = True
                break

            # gradient → check stops
            if not self.gradient_is_transparent(paint_server):
                visible = True
                break

            # gradient fully transparent → invisible
            continue

        return visible


    def resolve_geometry(self, node:BaseElement|None=None) -> None:
        """
        Resolve geometry nodes by removing invisible elements, converting unsupported
        shapes to <path>, and normalizing paint-order attributes, using a strict
        postorder traversal.

        This is the geometry-normalization pass that prepares the DOM for later
        pipeline stages such as transform flattening, marker expansion, clipPath
        resolution, and GT7-safe export. It performs three major tasks:

            1. Visibility pruning
            2. Unsupported-shape replacement
            3. Paint-order normalization

        It operates recursively and modifies the DOM in place.

        Pipeline
        --------

        1. **Visibility pruning**
        Before doing anything else, the function checks:

            is_geometry_visible(node)

        If the node is a geometry element and is fully invisible (no visible fill,
        no visible stroke, no visible gradient/pattern), it is removed:

            remove_node(node)

        This prevents invisible geometry from:
        - contributing to bounding boxes
        - receiving marker expansion
        - affecting paint-order resolution
        - polluting the final DOM

        The removal is logged.

        2. **Unsupported-shape replacement**
        The function calls:

            node, replaced_count = replace_unsupported_shape(node)

        This converts:
        - <rect> with rounded corners → <path>
        - <polyline> / <polygon> → <path>
        - <line> → <path>

        Replacement happens *before* recursion so children of the new node are
        processed correctly.

        3. **Postorder traversal**
        Children are processed first:

            for child in list(node):
                resolve_geometry(child)

        Paint-server containers are skipped:
            defs, styles, clipPath, pattern, mask, filter,
            linearGradient, radialGradient, meshgradient

        This ensures geometry is normalized before paint-server definitions are
        touched.

        4. **Paint-order normalization**
        After children are processed, geometry nodes receive:

            resolve_paint_order(node)

        This rewrites paint-order attributes into a canonical form and ensures
        correct layering for fill, stroke, and markers.

        5. **Root-level reporting**
        When `node == self.svg`, the function logs:
        - number of unsupported shapes converted
        - number of paint-order attributes normalized

        Why postorder?
        --------------
        Postorder guarantees:
        - invisible children are removed before parent decisions
        - unsupported shapes are replaced before paint-order resolution
        - paint-order normalization sees fully resolved geometry
        - no stale references remain after recursion

        Interaction with other passes
        -----------------------------
        This function is typically run after:

        - normalize_units()
        - resolve_references()
        - flatten_svg_dom()

        and before:

        - marker expansion
        - clipPath flattening
        - viewBox translation
        - GT7 export

        Parameters
        ----------
        node : BaseElement | None
            The node to process. If None, the root SVG element is used.

        Returns
        -------
        None
            The SVG DOM is modified in place.
        """

        replaced_count = 0
        paint_order_count = 0

        if node is None:
            node = self.svg

        if not self.is_geometry_visible(node):
            self.log(logging.INFO, f"Removed invisible geometry node {self.node_str(node)}")
            self.remove_node(node)
            return

        node, replaced_count = self.replace_unsupported_shape(node)

        # Postorder traversal: recurse children first, then the node itself

        for child in list(node):
            tag = self.tag_name(child)
            if tag in ("defs", "styles", "clipPath", "pattern", "mask", "filter", "linearGradient", "radialGradient", "meshgradient"): 
                continue

            self.resolve_geometry(child)

        # Process node itself after children have been processed

        tag = self.tag_name(node)

        match tag:

            case "g":
                pass

            case "path" | "rect" | "circle" | "ellipse" | "line" | "polyline" | "polygon":
                paint_order_count += self.resolve_paint_order(node)

            case "clippath":
                pass

        if node == self.svg:

            if replaced_count:
                self.log(logging.INFO, f"Converted {replaced_count} unsupported geometry nodes with paths")

            if paint_order_count:
                self.log(logging.INFO, f"Resolved {paint_order_count} attributes")


    def parse_paint_order(self, el:BaseElement, po:str|None = None) -> tuple[int, int, int]:
        """Parse the SVG paint-order for an element and determine the effective
        rendering order of fill, stroke, and markers.

        This resolves inherited paint-order, normalizes token sequences, and
        computes positional indices for each paint operation. It also applies
        visibility gating based on actual paint presence (color alpha, stroke
        width, fill opacity, marker attributes) and merges fill/stroke layers
        when both use the same visible color and are not separated by marker
        placement.

        Args:
            el: The SVG element whose paint-order should be evaluated.
            po: Optional explicit paint-order string. If omitted, the value is
                inherited from the element's attributes.

        Returns:
            tuple[int, int, int]: A 3-tuple (fill_pos, stroke_pos, marker_pos)
            where each entry is either:
                * a non-negative integer indicating its order, or
                * -1 if the corresponding paint operation is effectively absent.

        Behavior:
            * Invalid or missing paint-order defaults to (0, 1, 2).
            * Invisible fill/stroke (alpha == 0, width == 0, opacity == 0)
            forces their position to -1.
            * Missing markers (start/mid/end all “none”) set marker_pos to -1.
            * If fill and stroke share the same visible color and marker ordering
            does not separate them, equal values for stroke and fill are returned.
        """
        if not po:
            po = self.inherit_attribute(el, "paint-order")

        if not po:
            fill_pos = 0
            stroke_pos = 1
            marker_pos = 2
        
        else:
            # Normalize commas and whitespace
            normalized = re.sub(r"[,\s]+", " ", po.strip())

            # Extract valid paint-order tokens
            tokens = self.PAINT_ORDER_RE.findall(normalized)

            if not tokens:
                self.log(logging.DEBUG, f"Failed to parse paint-order='{po}'")
                return (0, 1, 2)

            # Compute positions
            fill_pos    = tokens.index("fill")    if "fill"    in tokens else -1
            stroke_pos  = tokens.index("stroke")  if "stroke"  in tokens else -1
            marker_pos  = tokens.index("markers") if "markers" in tokens else -1

        # Check presence of fill, stroke, and marker 

        fill = (self.inherit_attribute(el, "fill") or "none").strip().lower()
        fill_color, fill_alpha = self.parse_color(fill)
        if fill_alpha == 0:
            fill_pos = - 1

        stroke = (self.inherit_attribute(el, "stroke") or "none").strip().lower()
        stroke_color, stroke_alpha = self.parse_color(stroke)
        if stroke_alpha == 0:
            stroke_pos = -1

        marker_start = (self.inherit_attribute(el, "marker-start") or "none").lower()
        marker_mid = (self.inherit_attribute(el, "marker-mid") or "none").lower()
        marker_end = (self.inherit_attribute(el, "marker-end") or "none").lower()
        if marker_start == "none" and marker_mid == "none" and marker_end == "none":
            marker_pos = -1

        # If stroke of same color is used to make shape more bold, and both are not divided by marker_pos,
        # no need to split into two shapes, so treat stroke as same layer as fill
        if fill_color == stroke_color and ((marker_pos <= fill_pos and marker_pos <= stroke_pos) or (marker_pos >= fill_pos and marker_pos >= stroke_pos)):
            stroke_pos = fill_pos

        return (fill_pos, stroke_pos, marker_pos)


    def should_split_shapes(self, fill_pos:int, stroke_pos:int, marker_pos:int) -> bool:
        """
        Parse an SVG/CSS paint-order attribute into a canonical tuple
        (fill_pos, stroke_pos, marker_pos). The function interprets both the
        declared paint-order and the actual visual presence of fill, stroke, and
        markers, producing a stable ordering suitable for geometry normalization
        and shape splitting.

        The parser merges three layers of logic:
            1. Parsing the author-specified paint-order string.
            2. Visibility gating based on fill opacity, stroke opacity, stroke width,
                and alpha channels.
            3. Collapsing of fill and stroke layers when both share the same resolved
                color and markers do not lie between them.

        The returned tuple contains three integers representing the layer index of
        fill, stroke, and markers. A value of -1 indicates that the corresponding
        paint layer is not visually present.

        Processing steps:

        1. Obtain paint-order.
        If no explicit paint-order is provided, the attribute is inherited.
        If still absent, the default SVG order (fill=0, stroke=1, markers=2)
        is used.

        2. Normalize and parse tokens.
        Commas and whitespace are collapsed, and valid tokens ("fill",
        "stroke", "markers") are extracted. If parsing fails, the default order
        is used.

        3. Initial layer positions.
        Each token is assigned its index in the parsed list. Missing tokens
        receive -1.

        4. Visibility gating.
        Fill is removed if its alpha resolves to zero.
        Stroke is removed if its alpha resolves to zero.
        Markers are removed if marker-start, marker-mid, and marker-end are
        all "none".

        5. Stroke/fill collapsing.
        If fill and stroke resolve to the same color and markers do not divide
        their layers, stroke is collapsed onto the fill layer.

        6. Return canonical tuple.
        The final (fill_pos, stroke_pos, marker_pos) is returned.

        Examples:
            paint-order="stroke fill" → (1, 0, -1)
            fill="none", stroke="red" → (-1, 0, -1)
            fill="rgba(0,0,0,0)" → (-1, stroke_pos, marker_pos)
            fill="blue", stroke="blue" → stroke_pos collapsed to fill_pos

        Args:
            el (BaseElement): The element whose paint-order should be parsed.
            po (str | None): Optional explicit paint-order string.

        Returns:
            tuple[int, int, int]: Canonical (fill_pos, stroke_pos, marker_pos)
            layer indices.
        """

        # If either fill or stroke missing → cannot split
        if fill_pos < 0 or stroke_pos < 0:
            return False

        # Case A: stroke below fill → split
        if stroke_pos < fill_pos:
            return True

        # Case B: marker between stroke and fill → split
        if marker_pos >= 0 and ((stroke_pos < marker_pos < fill_pos) or (stroke_pos > marker_pos > fill_pos)):
            return True

        return False


    def resolve_paint_order(self, el:BaseElement) -> int:
        """
        Resolve the paint-order of a geometry element and determine the canonical
        ordering of fill, stroke, and markers. This function normalizes paint-order
        semantics and decides whether the element requires splitting into separate
        fill and stroke shapes.

        The function performs the following steps:

        1. Geometry check  
        Non-geometry nodes are ignored.

        2. Parse paint-order  
        Determine the canonical layer positions for fill, stroke, and markers.
        This incorporates declared paint-order, visibility gating, marker
        presence, and collapsing when fill and stroke colors match.

        3. Determine whether splitting is required  
        If splitting is unnecessary (e.g., stroke invisible, fill invisible,
        collapsed layers, or markers not requiring separation), the element is
        left untouched.

        4. Deep-copy geometry  
        Create two independent copies: a fill-only copy and a stroke-only copy.
        The stroke copy has its ID removed to avoid duplicates.

        5. Strip paint attributes  
        The fill copy removes stroke attributes.  
        The stroke copy removes fill attributes.

        6. Marker resolution  
        Markers must remain on exactly one copy.  
        The algorithm determines which layer is adjacent to the marker layer and
        removes marker attributes from the other copy.

        Cases include:  
        - markers below both → keep markers on the lower layer  
        - markers above both → keep markers on the higher layer  
        - markers between fill and stroke → keep markers on the top layer  

        7. DOM insertion  
        The two copies replace the original node at the same index, ordered
        according to paint-order.

        8. Marker-style annotation  
        Both copies receive a synthetic “marker-style” attribute containing the
        original fill, stroke, and stroke-width.

        9. Remove original  
        The original node is removed from the DOM.

        10. Return  
        Returns 1 if a split occurred, otherwise 0.

        Args:
            el (BaseElement):  
                The geometry element whose paint-order should be resolved.

        Returns:
            int:  
                1 if the element was split into fill/stroke copies, otherwise 0.
        """

        if not self.is_geometry(el, only_gt7_supported=False):
            return 0
        
        # --- Parse paint-order into tuple ---
        fill_pos, stroke_pos, marker_pos = self.parse_paint_order(el)

        if not self.should_split_shapes(fill_pos, stroke_pos, marker_pos):
            return 0
        
        parent = el.getparent()
        if parent is None:
            return 0

        # --- Deep copies ---
        fill = copy.deepcopy(el)
        stroke = copy.deepcopy(el)
        stroke.attrib.pop("id", None)

        # --- Remove stroke attributes from fill copy ---
        for a in ["stroke", "stroke-width"]:
            fill.attrib.pop(a, None)

        # --- Remove fill attributes from stroke copy ---
        for a in ["fill"]:
            stroke.set("fill", "none")

        # --- Marker resolution ---
        # If markers exist, remove marker attributes from the copy
        # that is NOT adjacent to marker_pos.
        # Determine which paint layer is immediately above markers
        if marker_pos < fill_pos and marker_pos < stroke_pos:
            # markers bottommost → keeper is the smaller of fill_pos, stroke_pos
            drop_markers = stroke if fill_pos < stroke_pos else fill

        elif marker_pos > fill_pos and marker_pos > stroke_pos:
            # markers topmost → keeper is the larger of fill_pos, stroke_pos
            drop_markers = stroke if fill_pos > stroke_pos else fill

        else:
            # markers between fill and stroke → keeper is the top layer
            
            drop_markers = stroke if stroke_pos > fill_pos else fill

        for a in ["marker-start", "marker-mid", "marker-end"]:
            drop_markers.attrib.pop(a, None)

        # --- Insert in correct DOM order ---
        idx = parent.index(el)

        if (stroke_pos < fill_pos):
            # stroke is bottom, fill is top
            self.add_node(stroke, parent, idx)
            self.add_node(fill, parent, idx + 1)
        else:
            # stroke is top, fill is bottom
            self.add_node(stroke, parent, idx + 1)
            self.add_node(fill, parent, idx)

        # Add special style to stroke and fill to resolve marker correctly later
        fill_attr = el.get("fill", "none")
        stroke_attr = el.get("stroke", "none")
        stroke_width_attr = el.get("stroke-width", "1.0")
        style = f"fill:{fill_attr};stroke:{stroke_attr};stroke-width:{stroke_width_attr}"
        stroke.set("marker-style", style + ";role:stroke")
        fill.set("marker-style", style + ";role:fill")

        # --- Remove original ---
        self.remove_node(el, parent)

        return 1


    def is_empty_path(self, p:PathElement) -> bool:
        """
        Determine whether a PathElement is empty.

        A path is considered empty when it contains no drawable geometry commands.
        This function performs a minimal, strict check on the `d` attribute of a
        <path> element and is used by geometry-normalization passes to quickly
        identify paths that contribute no visible output.

        An element is considered empty if:
            1. The element itself is None.
            2. The `d` attribute is missing, None, or an empty string.
            3. The `d` attribute contains only whitespace.

        Any other value — even syntactically invalid but non-empty content — is
        treated as non-empty to keep the check fast and predictable.

        Empty paths commonly appear in SVGs generated by editors and automated
        pipelines due to deleted shapes, clipping artifacts, marker expansion,
        boolean operations, or placeholder nodes in <defs>. Downstream passes use
        this check to prune invisible or meaningless geometry early.

        Args:
            p (PathElement): The <path> element to inspect.

        Returns:
            bool: True if the path is empty, False otherwise.
        """

        if p is None:
            return True
        return (p.get("d") or "").strip() == ""
    
    
    def combine_paths(self, paths:list[PathElement]) -> PathElement|None:
        """
        Combine multiple <path> elements into a single <path> by concatenating their
        geometry and preserving the dominant fill-rule / clip-rule semantics.

        This helper performs a **pure geometric merge**: it does not attempt boolean
        union, intersection, or winding-rule correction. It simply concatenates the
        `d` attributes of all provided paths and applies consistent fill/clip rules.

        It is used in lightweight merging stages (tile merging, signature merging,
        path coalescing) where the caller already knows that concatenation is safe.

        Behavior
        --------

        1. **Empty input**
        If `paths` is empty, the function returns None.

        2. **Concatenate geometry**
        The `d` attributes of all non-None paths are concatenated with a space:

            d = " ".join(p.get("d") or "")

        This preserves command boundaries and avoids accidental token merging.

        3. **Create merged path**
        A new `inkex.PathElement()` is created and assigned the concatenated `d`.

        4. **Resolve fill-rule**
        The function inspects all `fill-rule` attributes in the input paths:

            fill_rules = {"evenodd", "nonzero", ...}

        Priority:
        - If any path uses **evenodd**, the merged path uses evenodd.
        - Else if any path uses **nonzero**, the merged path uses nonzero.
        - Else no fill-rule is set.

        This matches SVG's rule that evenodd overrides nonzero when mixing
        subpaths.

        5. **Resolve clip-rule**
        Same logic as fill-rule:
        - evenodd wins over nonzero
        - nonzero wins over absence

        6. **Return merged path**
        The merged <path> is returned.

        Limitations
        -----------
        - This is **not** a boolean union. It does not compute winding numbers.
        - It does not normalize transforms; callers must flatten first.
        - It does not copy presentation attributes (fill, stroke, etc.).
        - It does not remove empty subpaths; callers should use is_empty_path() if needed.

        Use cases
        ---------
        - Tile merging in pattern resolution
        - Signature merging in meshgradient evaluation
        - Lightweight path coalescing before GT7 export
        - Combining adjacent geometry fragments after boolean operations

        Parameters
        ----------
        paths : list[PathElement]
            A list of <path> elements to merge.

        Returns
        -------
        PathElement | None
            A new merged <path> element, or None if the input list is empty.
        """

        if not paths:
            return None

        # Concatenate path data
        d = " ".join((p.get("d") or "") for p in paths if p is not None)

        merged = inkex.PathElement()
        merged.set("d", d)

        # Determine fill-rule
        fill_rules = {p.get("fill-rule") for p in paths if p.get("fill-rule")}
        clip_rules = {p.get("clip-rule") for p in paths if p.get("clip-rule")}

        # Apply correct fill-rule
        if "evenodd" in fill_rules:
            merged.set("fill-rule", "evenodd")
        elif "nonzero" in fill_rules:
            merged.set("fill-rule", "nonzero")

        # Apply correct clip-rule
        if "evenodd" in clip_rules:
            merged.set("clip-rule", "evenodd")
        elif "nonzero" in clip_rules:
            merged.set("clip-rule", "nonzero")

        return merged

    
    def empty_path(self) -> PathElement:
        """
        Create and return a canonical empty <path> element.

        Constructs a PathElement whose `d` attribute is an empty string. This is
        used throughout geometry-normalization and path-merging logic whenever a
        placeholder, sentinel, or “no geometry” path is required.

        The function is intentionally minimal:
            - A new `inkex.PathElement()` is created.
            - Its `d` attribute is set to "".
            - No presentation, transform, or style attributes are added.
            - The element is not inserted into the DOM.
            - No validation or normalization is performed.

        Typical use cases include:
            - Placeholder return values when geometry cannot be computed.
            - Safe defaults in path-combining logic.
            - Sentinel values for “no geometry” in boolean operations.
            - Fallbacks when unsupported shapes fail conversion.
            - Neutral elements in GT7-safe export pipelines.

        Args:
            None.

        Returns:
            PathElement: A new <path> element with `d=""`.
        """

        p = inkex.PathElement()
        p.set("d", "")
        return p


    def normalize_path(self, path: Path) -> Path:
        """
        Normalize an inkex Path by converting all commands to absolute coordinates,
        expanding shorthand commands, and ensuring that every command exposes a
        valid `.end` endpoint. This produces a stable, predictable path structure
        for downstream geometry processing.

        This function is the canonical low-level path normalizer used before
        subpath iteration, endpoint extraction, marker expansion, and paint-order
        splitting.

        Normalization steps:
            1. Absolute coordinate conversion  
            Calling `path.to_absolute()` rewrites all commands (M, L, C, Q, A, etc.)
            into absolute form. This removes relative-coordinate ambiguity and
            ensures later geometry passes operate on stable, global coordinates.

            2. Shorthand expansion  
            Calling `p.to_non_shorthand()` expands shorthand commands:
                - S → cubic Bézier with explicit control points  
                - T → quadratic Bézier with explicit control points  
                - H/V → horizontal/vertical lines rewritten as L commands  
            This guarantees that every command has explicit parameters and a
            well-defined endpoint.

            3. Endpoint guarantee  
            After absolute conversion and shorthand expansion, all commands in the
            resulting Path object expose a valid `.end` attribute. This is required
            for subpath iteration, transform flattening, and marker placement.

        Error handling:
            If normalization fails due to malformed paths, unsupported commands, or
            corrupted SVG data, the function logs the failure and returns an empty
            `Path()`. This sentinel allows downstream passes to detect broken geometry
            via emptiness checks or zero-segment iteration, preventing crashes and
            propagation of invalid data.

        Args:
            path (Path): An inkex Path instance (not a PathElement). Typically obtained
                via `path = pathElement.path`.

        Returns:
            Path: A normalized Path object, or an empty Path() if normalization fails.
        """

        try:
            # Convert to absolute coordinates
            p = path.to_absolute()

            # Expand shorthand commands (S, T, H, V)
            p = p.to_non_shorthand()

            return p

        except Exception as e:
            self.log(logging.DEBUG, f"Invalid path {str(path)}")
            self.log(logging.DEBUG, str(e))
            # If normalization fails, return empty list
            return Path()


    def end_point(self, cmd:PathCmd, sub_start:None|complex) -> complex:
        """
        Return the endpoint of a normalized path command as a complex(x, y) pair,
        handling Z-commands, args-based fallbacks, and malformed commands safely.

        This function is the **unified endpoint accessor** used throughout the
        subpath-iteration and geometry-normalization pipeline. After a path has been
        normalized via normalize_path(), every
        command *should* expose a valid `.end` attribute — but real-world SVGs
        frequently contain malformed or partially normalized commands. This helper
        provides a robust, predictable fallback strategy.

        Resolution rules
        ----------------

        1. **Preferred: `.end` attribute**
        Inkscape's normalized PathCmd objects always provide:

            cmd.end.x
            cmd.end.y

        If present, this is the authoritative endpoint.

        2. **Z command (closepath)**
        A `Z` command closes the current subpath and returns to the subpath's
        start point:

            return sub_start

        If `sub_start` is unexpectedly None (malformed path), the function
        returns `complex(0, 0)` as a safe sentinel.

        3. **Fallback: `.args`**
        Some commands (especially those produced by older Inkscape versions or
        partially normalized SVGs) may not have `.end` but still store their
        endpoint in the last two arguments:

            x = cmd.args[-2]
            y = cmd.args[-1]

        This fallback ensures compatibility with legacy or corrupted paths.

        4. **Malformed command**
        If neither `.end` nor `.args` provides a usable endpoint, the function
        logs a warning and returns:

            complex(0, 0)

        This prevents crashes in downstream passes such as:
        - marker expansion
        - paint-order splitting
        - GT7-safe export

        Why this matters
        ----------------
        Endpoint resolution is foundational for:
        - subpath segmentation
        - bounding-box computation
        - marker placement
        - transform flattening
        - path merging and splitting

        A single malformed command must not break the pipeline. This helper ensures
        that every command yields a usable endpoint, even if degraded.

        Parameters
        ----------
        cmd : PathCmd
            The command whose endpoint should be resolved.
        sub_start : complex | None
            The start point of the current subpath, required for Z commands.

        Returns
        -------
        complex
            The resolved endpoint, or (0+0j) if the command is malformed.
        """

        # Preferred: Inkscape-normalized commands always have .end
        if hasattr(cmd, "end") and cmd.end is not None:
            return complex(cmd.end.x, cmd.end.y)

        letter = cmd.letter.upper()

        # Z closes the subpath
        if letter == "Z":
            return sub_start if sub_start is not None else complex(0, 0)

        # Fallback: Inkscape stores parameters in .args
        if hasattr(cmd, "args") and len(cmd.args) >= 2:
            x = cmd.args[-2]
            y = cmd.args[-1]
            return complex(x, y)

        # Truly malformed command
        self.log(logging.WARNING,
                f"Malformed command {cmd.letter} without endpoint: {cmd}")
        
        return complex(0,0)
    

    def iter_subpaths(self, path:Path) -> Iterator[tuple[complex, list[PathCmd]]]:
        """Iterate over all subpaths in an SVG path and yield their start point
        and command list.

        A subpath begins at each absolute or relative “M” (moveto) command.
        Subsequent drawing commands (L, C, Q, A, H, V, Z, etc.) belong to that
        subpath until the next “M” is encountered. Each yielded tuple contains:

            (start_point, [commands_in_subpath])

        The method also performs defensive validation:
            * If an “M” command lacks a valid endpoint, the subpath is skipped
            and a warning is logged.
            * Commands before the first valid “M” are ignored.
            * The endpoint of each command is resolved using unified endpoint
            logic, ensuring consistent handling of absolute/relative commands.

        Args:
            path: A Path object containing ordered PathCmd instances.

        Returns:
            Iterator[tuple[complex, list[PathCmd]]]:
                An iterator over all detected subpaths, each represented by its
                starting complex coordinate and its list of drawing commands.
        """
        current = []
        start_pt = None
        prev_pt = None

        for cmd in path:
            letter = cmd.letter.upper()

            if letter == "M":
                # Close previous subpath if any
                if current and start_pt is not None:
                    yield (start_pt, current)

                current = []

                # IMPORTANT: M endpoint must be computed from the M command itself
                start_pt = self.end_point(cmd, None)

                if start_pt is None:
                    self.log(logging.WARNING,
                            f"[PAT] Malformed path: M command without endpoint in {self.node_str(cmd)}")
                    prev_pt = None
                    continue

                prev_pt = start_pt
                continue

            # Ignore commands before first M
            if prev_pt is None:
                continue

            # Add command to current subpath
            current.append(cmd)

            # Update prev_pt using unified endpoint logic
            prev_pt = self.end_point(cmd, start_pt)

        # Final subpath
        if current and start_pt:
            self.log(logging.DEBUG,
                    f"[PAT] Yield ({start_pt.real}, {start_pt.imag}), {current}")
            yield (start_pt, current)

    
    def is_subpath_closed(self, start_pt:complex, cmds:List[PathCmd]) -> bool:
        """Determine whether a subpath is closed either syntactically or geometrically.

        A subpath is considered closed if:
            * It contains a “Z”/“z” closepath command, or
            * Its final computed endpoint coincides with its start point
            within a small numerical tolerance.

        The method evaluates both conditions because some normalization
        pipelines or authoring tools may replace an explicit “Z” with an
        equivalent final lineto back to the start point.

        Args:
            start_pt: The complex coordinate of the subpath's starting point.
            cmds: The list of PathCmd objects belonging to the subpath.

        Returns:
            bool: True if the subpath is closed, False otherwise.
        """

        closed_by_Z = any(cmd.letter.upper() == "Z" for cmd in cmds)

        # Compute final point
        last_pt = start_pt
        for cmd in cmds:
            last_pt = self.end_point(cmd, start_pt)

        geom_closed = abs(last_pt - start_pt) < 1e-9

        return closed_by_Z or geom_closed


    def is_stroke(self, path:PathElement) -> bool:
        """Determine whether a path element contains at least one open subpath.

        This method normalizes the path data, iterates over all detected
        subpaths, and checks each one for closure. A path is considered a
        “stroke-only” shape if any of its subpaths is open — meaning it lacks
        a “Z” closepath command and its final endpoint does not coincide with
        its starting point.

        Args:
            path: The PathElement whose geometry should be inspected.

        Returns:
            bool: True if at least one subpath is open (stroke-only), False if
            all subpaths are closed.
        """

        tag = self.tag_name(path)
        if tag != "path":
            return False
        
        p = self.normalize_path(path.path)
        if not p:
            return False

        for (start_pt, cmds) in self.iter_subpaths(p):
            if not self.is_subpath_closed(start_pt, cmds):
                return True   # at least one subpath is open

        return False          # all subpaths closed


    def convert_to_path(self, node:BaseElement, transform:Transform|None=None, replace_node:bool=False) -> PathElement:
        """Convert any supported SVG geometry element into a normalized PathElement.

        This routine unifies all geometric primitives (path, rect, circle,
        ellipse, polygon, polyline, line) into a single path representation.
        It resolves transforms, preserves presentation attributes, and can
        optionally replace the original node in the document tree.

        Behavior:
            * For existing <path> elements:
                - Convert to absolute coordinates.
                - Apply the provided transform (if any).
                - Clone into a new PathElement while preserving style,
                fill-rule, and other presentation attributes.
            * For other geometry types:
                - Delegate to the corresponding shape-to-path converter
                (rect_to_path, circle_to_path, etc.).
            * Unsupported geometry types return None.

        Args:
            node: The SVG element to convert.
            transform: Optional transform to apply during conversion.
            replace_node: If True, the original node is removed and replaced
                in its parent with the newly created PathElement.

        Returns:
            PathElement | None: The converted path element, or None if the
            geometry type is unsupported.
        """
        
        self.log(logging.DEBUG, f"OLD NODE {self.node_str(node)}") 

        tag = self.tag_name(node)

        new_node = None

        if tag == "path":

            # Parse existing path, preserving all subpaths
            p = node.path.to_absolute()

            self.log(logging.DEBUG,
                f"[CTP] BEFORE transform: {str(p)}"
            )

            # Apply transform safely
            if transform is not None:
                p = p.transform(transform)

            self.log(logging.DEBUG,
                f"[CTP] AFTER transform: {str(p)}"
            )

            # Create new node (clone) or replacement
            new_node = inkex.PathElement()
            new_node.path = p

            # Copy presentation attributes (fill, stroke, opacity, etc.)
            self.copy_presentation_attributes(node, new_node)

            # Preserve fill-rule
            if node.get("fill-rule"):
                new_node.set("fill-rule", node.get("fill-rule"))

            if node.get("style"):
                new_node.set("style", node.get("style"))

            if replace_node:
                new_node.attrs.pop("id", None)  # Remove ID to avoid duplicates
                parent, idx = self.parent_of(node)
                self.remove_node(node, parent)
                self.add_node(new_node, parent, idx)

        else:
        
            self.log(logging.DEBUG, f"[CTP] {self.node_str(node)}")

            if tag == "rect":
                new_node = self.rect_to_path(node, transform=transform, replace_node=replace_node)

            if tag == "circle":
                new_node = self.circle_to_path(node, transform=transform, replace_node=replace_node)

            if tag == inkex.addNS('ellipse', 'svg'):
                new_node = self.ellipse_to_path(node, transform=transform, replace_node=replace_node)

            if tag in ("polygon", "polyline"):
                new_node = self.poly_to_path(node, transform=transform, replace_node=replace_node)

            if tag == "line":
                new_node = self.line_to_path(node, transform=transform, replace_node=replace_node)

        # Unsupported geometry → ignore
        if new_node is None:
            self.log(logging.DEBUG, f"Unsupported {self.node_str(node)}")
        else:
            self.log(logging.DEBUG, f"NEW NODE {self.node_str(new_node)}")    
        return new_node


    def circle_to_path(self, node:BaseElement, transform:Transform|None=None, replace_node:bool=True) -> PathElement:
        """Convert an SVG <circle> element into an equivalent <path> element.

        The circle is represented using two arc commands forming a complete
        closed loop. The method extracts the circle's geometric parameters,
        constructs the corresponding path data, applies any provided transform,
        and preserves all presentation attributes (fill, stroke, opacity, etc.).
        Optionally, the original <circle> node can be replaced in the document
        tree.

        Args:
            node: The <circle> element to convert.
            transform: Optional transform applied to the generated path.
            replace_node: If True, the original <circle> is removed and replaced
                with the new <path> element.

        Returns:
            PathElement: The newly created path representing the circle.
        """

        # Extract geometry
        cx = float(node.get("cx") or "0")
        cy = float(node.get("cy") or "0")
        r  = float(node.get("r") or  "0")

        # Build path data for a circle using two arcs
        d = (
            f"M {cx - r},{cy} "
            f"a {r},{r} 0 1,0 {2*r},0 "
            f"a {r},{r} 0 1,0 {-2*r},0"
        )

        # Convert to path and apply transform
        # Convert to path and apply transform
        p = inkex.Path(d).to_absolute()  # type: ignore

        if transform is not None:
            p = p.transform(transform)

        # Create new <path> element
        new_node = inkex.PathElement()
        new_node.set("d", str(p))

        # Copy presentation attributes
        self.copy_presentation_attributes(node, new_node)
        self.scale_stroke_width(new_node, transform)

        # Replace <circle> with <path>
        if replace_node:
            parent, idx = self.parent_of(node)
            self.remove_node(node)
            self.add_node(new_node, parent, idx)

        return new_node


    def rect_to_path(self, node:BaseElement, transform:Transform|None=None, replace_node:bool=True) -> PathElement:
        """Convert an SVG <rect> (with or without rounded corners) into a <path> element.

        The method extracts the rectangle's geometry, clamps corner radii to
        valid ranges, constructs the corresponding path data (either a simple
        rectangular loop or a rounded-rectangle composed of straight segments
        and arc commands), applies any provided transform, and preserves all
        presentation attributes. Optionally, the original <rect> node can be
        replaced in the document tree.

        Args:
            node: The <rect> element to convert.
            transform: Optional transform applied to the generated path.
            replace_node: If True, the original <rect> is removed and replaced
                with the new <path> element.

        Returns:
            PathElement: The newly created path representing the rectangle.
        """

        # Extract geometry
        x = float(node.get("x") or "0")
        y = float(node.get("y") or "0")
        w = float(node.get("width") or  "0")
        h = float(node.get("height") or "0")

        rx = float(node.get("rx", "0") or "0")
        ry = float(node.get("ry", "0") or "0")

        # Clamp radii
        rx = max(0.0, min(rx, w / 2.0))
        ry = max(0.0, min(ry, h / 2.0))

        # Build path data
        if rx == 0 and ry == 0:
            d = f"M {x},{y} h {w} v {h} h {-w} z"
        else:
            d = (
                f"M {x+rx},{y} "
                f"H {x+w-rx} "
                f"A {rx},{ry} 0 0 1 {x+w},{y+ry} "
                f"V {y+h-ry} "
                f"A {rx},{ry} 0 0 1 {x+w-rx},{y+h} "
                f"H {x+rx} "
                f"A {rx},{ry} 0 0 1 {x},{y+h-ry} "
                f"V {y+ry} "
                f"A {rx},{ry} 0 0 1 {x+rx},{y} "
                f"Z"
            )

        # Convert to path and apply transform
        p = inkex.Path(d).to_absolute()  # type: ignore

        if transform is not None:
            p = p.transform(transform)

        # Create new <path> element
        new_node = inkex.PathElement()
        new_node.set("d", str(p))

        # Copy presentation attributes
        self.copy_presentation_attributes(node, new_node)
        self.scale_stroke_width(new_node, transform)

        # Replace <rect> with <path>
        if replace_node:
            parent, idx = self.parent_of(node)
            self.remove_node(node)
            self.add_node(new_node, parent, idx)

        return new_node

        
    def ellipse_to_path(self, node:BaseElement, transform:Transform|None=None, replace_node:bool=True) -> PathElement:
        """Convert an SVG <ellipse> element into an equivalent <path> element.

        The ellipse is represented using two arc commands forming a complete
        closed loop. The method extracts the ellipse's geometric parameters,
        constructs the corresponding path data, applies any provided transform,
        and preserves all presentation attributes (fill, stroke, opacity, etc.).
        Optionally, the original <ellipse> node can be replaced in the document
        tree.

        Args:
            node: The <ellipse> element to convert.
            transform: Optional transform applied to the generated path.
            replace_node: If True, the original <ellipse> is removed and replaced
                with the new <path> element.

        Returns:
            PathElement: The newly created path representing the ellipse.
        """

        # Extract geometry
        cx = float(node.get("cx") or "0")
        cy = float(node.get("cy") or "0")
        rx = float(node.get("rx") or "0")
        ry = float(node.get("ry") or "0")

        # Build ellipse path using two arcs
        d = (
            f"M {cx - rx},{cy} "
            f"a {rx},{ry} 0 1,0 {2*rx},0 "
            f"a {rx},{ry} 0 1,0 {-2*rx},0"
        )

        # Convert to path and apply transform
        p = inkex.Path(d).to_absolute()  # type: ignore

        if transform is not None:
            p = p.transform(transform)

        # Create new <path> element
        new_node = inkex.PathElement()
        new_node.set("d", str(p))

        # Copy presentation attributes
        self.copy_presentation_attributes(node, new_node)
        self.scale_stroke_width(new_node, transform)

        # Replace <ellipse> with <path>
        if replace_node:
            parent, idx = self.parent_of(node)
            self.remove_node(node)
            self.add_node(new_node, parent, idx)

        return new_node


    def poly_to_path(self, node:BaseElement, transform:Transform|None=None, replace_node:bool=True) -> PathElement:
        """Convert an SVG <polygon> or <polyline> element into a <path> element.

        The method parses the element's “points” attribute, constructs a
        corresponding path consisting of moveto/lineto commands, and closes
        the path when the element is a <polygon>. It then applies any provided
        transform, preserves presentation attributes, and optionally replaces
        the original node in the document tree.

        Args:
            node: The <polygon> or <polyline> element to convert.
            transform: Optional transform applied to the generated path.
            replace_node: If True, the original node is removed and replaced
                with the new <path> element.

        Returns:
            PathElement: The newly created path representing the polygon or
            polyline, or an empty path if the input contains insufficient
            coordinate data.
        """

        points = node.get("points")
        if not points:
            return self.empty_path()

        coords = [float(v) for v in re.split(r"[ ,]+", points.strip()) if v]
        if len(coords) < 2:
            return self.empty_path()

        # Build path commands
        d = []
        for i in range(0, len(coords), 2):
            x, y = coords[i], coords[i + 1]
            if i == 0:
                d.append(f"M {x},{y}")
            else:
                d.append(f"L {x},{y}")

        close = (self.tag_name(node) == "polygon")

        if close:
            d.append("Z")

        # Apply transform
        p = inkex.Path(" ".join(d)).to_absolute() # type: ignore

        if transform is not None:
            p = p.transform(transform)

        # Create new <path> element
        new_node = inkex.PathElement()
        new_node.set("d", str(p))

        # Copy presentation attributes
        self.copy_presentation_attributes(node, new_node)
        self.scale_stroke_width(new_node, transform)

        # Replace original node
        if replace_node:
            parent, idx = self.parent_of(node)
            self.remove_node(node)
            self.add_node(new_node, parent, idx)

        return new_node


    def line_to_path(self, node:BaseElement, transform:Transform|None=None, replace_node:bool=True) -> PathElement:
        """Convert an SVG <line> element into a <path> element.

        The method extracts the line's endpoints, constructs a simple moveto/lineto
        path, applies any provided transform, and preserves all presentation
        attributes (fill, stroke, opacity, etc.). It also scales stroke width when
        transforms are present. Optionally, the original <line> node can be replaced
        in the document tree.

        Args:
            node: The <line> element to convert.
            transform: Optional transform applied to the generated path.
            replace_node: If True, the original <line> is removed and replaced
                with the new <path> element.

        Returns:
            PathElement: The newly created path representing the line.
        """

        x1 = float(node.get("x1") or "0")
        y1 = float(node.get("y1") or "0")
        x2 = float(node.get("x2") or "0")
        y2 = float(node.get("y2") or "0")

        d = f"M {x1},{y1} L {x2},{y2}"

        # Apply transform
        p = inkex.Path(d).to_absolute()  # type: ignore

        if transform is not None:
            p = p.transform(transform)

        # Create new <path> element
        new_node = inkex.PathElement()
        new_node.set("d", str(p))

        # Copy presentation attributes
        self.copy_presentation_attributes(node, new_node)
        self.scale_stroke_width(new_node, transform)

        # Replace original node
        if replace_node:
            parent, idx = self.parent_of(node)
            self.remove_node(node)
            self.add_node(new_node, parent, idx)

        return new_node


    def apply_transform_to_gradients_used_by(self, node:BaseElement, T:Transform) -> int:
        """Apply a coordinate-system transform to all gradients referenced by an element.

        This method inspects the element's fill and stroke paint attributes,
        resolves any linked gradient paint servers (linear, radial, mesh),
        and applies the provided transform matrix to each gradient's geometric
        coordinates. It assumes all gradients are already chain-resolved and
        expressed in userSpaceOnUse, making the operation GT7-safe.

        Args:
            node: The SVG element whose referenced gradients should be updated.
            T: The transform to apply to each gradient's coordinate attributes.

        Returns:
            int: The number of gradients successfully transformed.
        """

        count = 0

        for attr in ("fill", "stroke"):

            grad, _ = self.ref_target(node, attr, tag_name={"meshgradient", "linearGradient", "radialGradient"})
            if grad is None:
                continue

            self.log(logging.DEBUG,f"Apply transform {T} on {self.node_str(grad)} for {self.node_str(node)}")

            # Apply CTM to gradient geometry
            self.apply_transform_to_gradient(grad, T)

            self.log(logging.DEBUG, f"Transformed gradient {self.node_str(grad)}")

            count += 1

        return count


    def stroke_scale(self, el:BaseElement, transform:Transform) -> float:
        """Compute the effective stroke-width scaling factor induced by a transform.

        This method inspects the element's `vector-effect` attribute and the
        transform matrix to determine whether stroke width should scale and, if
        so, by how much. It uses the geometric mean of the transform's X/Y scale
        components to approximate a uniform stroke scaling factor.

        Rules:
            * If `vector-effect="non-scaling-stroke"` → return 0 (stroke must not scale).
            * If the transform contains no scaling (scale ≈ 1) → return 0.
            * Otherwise → return the computed scale factor.

        The returned value is *only* the scale factor. The caller is responsible
        for applying it to the element's stroke-width.

        Args:
            el: The SVG element whose stroke scaling behavior is evaluated.
            transform: The transform whose matrix components determine scaling.

        Returns:
            float: The stroke scaling factor, or 0 if stroke width should not scale.
        """

        self.log(logging.DEBUG, f"{self.node_str(el)}, transform={transform}")

        # 1. Check vector-effect
        ve = el.get("vector-effect")
        if ve == "non-scaling-stroke":
            return 0   # nothing to do

        # 2. Extract matrix
        (a, c, e), (b, d, f) = transform.matrix # pyright: ignore[reportUnusedVariable]

        # 3. Compute uniform-ish scale factor
        #    For pure scale(sx,sy): sqrt(|sx*sy|)
        sx = (a * a + b * b) ** 0.5
        sy = (c * c + d * d) ** 0.5

        # Geometric mean as uniform-ish scale
        scale = (sx * sy) ** 0.5

        # If no scaling → nothing to do
        if abs(scale - 1.0) < 1e-12:
            self.log(logging.DEBUG, f" [TRA] Ignoring {self.node_str(el)}, factor={scale}")
            return 0

        return scale
    

    def scale_stroke_width(self, el:BaseElement, transform:Transform|None) -> float:
        """
        Apply explicit stroke-width scaling based on an element's transform.

        This function converts SVG's implicit stroke-scaling behavior into an
        explicit numeric ``stroke-width`` value. When flattening transforms,
        geometric scaling must be preserved while ``vector-effect`` attributes
        are removed. This ensures that the final stroke width matches what the
        renderer would have produced.

        Scaling logic:
            If ``transform`` is ``None``, no scaling is applied and the function
            returns ``1.0``.

            If ``vector-effect="non-scaling-stroke"``, the function returns ``0``
            and the stroke width is not modified.

            If the transform matrix contains no scale component, the function
            returns ``0``.

            Otherwise, the scale factor is computed via ``stroke_scale``. The
            ``vector-effect`` attribute is removed, the inherited stroke width
            (default ``1``) is multiplied by the scale factor, and the updated
            value is written back to the element.

        Args:
            el (BaseElement): The SVG element whose stroke width should be scaled.
            transform (Transform | None): The transform applied to the element.

        Returns:
            float: The computed scale factor. ``0`` indicates that no stroke
            scaling was applied. A value greater than zero indicates that the
            stroke width was updated.
        """


        if transform is None:
            return 1.0

        # 1. Check vector-effect
        scale = self.stroke_scale(el, transform=transform)

        if scale > 1e-12:
            # 4. Remove vector-effect
            el.attrib.pop("vector-effect", None)

            # 5. Adjust stroke-width
            sw_raw = self.inherit_attribute(el, "stroke-width")
            sw = float(sw_raw) if sw_raw is not None else 1.0
            el.set("stroke-width", str(sw * scale))

            self.log(logging.DEBUG, f" [TRA] Scaled {self.node_str(el)}, factor= {scale}, stroke-width {sw} --> {sw*scale}")

        return scale
    

    def apply_transform_to_node(self, node:BaseElement, transform:Transform) -> tuple[int, BaseElement]:
        """Apply a geometric transform to a supported SVG geometry element.

        This method dispatches the element to the appropriate shape-specific
        transform routine (path, circle, ellipse, rect). Unsupported geometry
        types return ``(0, node)`` without modification. After the transform is
        applied, the element's ``transform`` attribute is removed and any
        referenced gradients are updated to reflect the new coordinate system.

        Error handling:
            * If a transform routine raises an exception, the method logs the
            failure and returns ``(0, node)``.
            * On success, the method returns ``(1, node)`` with the updated
            geometry.

        Args:
            node (BaseElement):
                The SVG element to transform.
            transform (Transform):
                The transform to apply to the element.

        Returns:
            tuple[int, BaseElement]:
                A pair ``(status, node)`` where:
                    * ``status = 1`` indicates successful transformation.
                    * ``status = 0`` indicates that the element was unsupported
                    or an error occurred.
                The second value is the (possibly modified) element itself.
        """
        
        tag = self.tag_name(node)

        self.log(logging.DEBUG, f"Transform {self.node_str(node)}, transform={transform} ")

        try:
            match tag:
                case "path":
                    node = self.transform_path(node, transform)

                case "circle":
                    node = self.transform_circle(node, transform)

                case "ellipse":
                    node = self.transform_ellipse(node, transform)

                case "rect":
                    node = self.transform_rect(node, transform)

                case _:
                    return (0,node)

            node.attrib.pop("transform", None)

            self.apply_transform_to_gradients_used_by(node, transform)

            self.log(logging.DEBUG, f"Transformed to {self.node_str(node)}")

        except Exception as e:
            self.log(logging.WARNING, f"Node transform failed for {self.node_str(node)} ")
            self.log(logging.WARNING, f"tag={node.tag} attrib={dict(node.attrib)}")
            
            self.log(logging.WARNING, f"exception: {type(e).__name__}: {e}")
            self.log(logging.WARNING, traceback.format_exc())
            return (0, node)

        return (1, node)

        
    def flatten_group(self, g:Group) -> None:
        """Flatten a <g> group by promoting inheritable presentation attributes
        and reparenting its drawable children.

        This routine removes structural grouping while preserving visual
        semantics. It copies inheritable presentation attributes (fill, stroke,
        opacity, etc.) from the group to its drawable children when those
        attributes are not already set, then moves each child directly into the
        group's parent. Non-drawable children (e.g., defs, metadata) are ignored.
        After all drawable children are reparented, the now-empty group is
        removed.

        Args:
            g (Group):
                The <g> element to flatten.

        Returns:
            None
                The group is removed in-place; no value is returned.
        """

        self.log(logging.DEBUG, f"Flattening {self.node_str(g)}")

        # Collect inheritable presentation attributes
        group_attrs = {
            k: v for k, v in g.attrib.items()
            if k in self.PRESENTATION_ATTRS
        }

        # Modern parent lookup
        parent, idx = self.parent_of(g)

        # Iterate over a snapshot because children will be moved
        for child in list(g):
            # Only flatten drawable shapes
            if not isinstance(child, inkex.ShapeElement):
                continue

            # Promote presentation attributes
            for attr, val in group_attrs.items():
                if child.get(attr) is None:
                    child.set(attr, val)

            # Reparent child using modern DOM helpers
            self.remove_node(child, g)
            self.add_node(child, parent, idx)
            idx += 1

        # Remove the now-empty group
        self.remove_node(g, parent)

    # endregion

    # region --- Cleanup SVG Tree ----
        
    def collect_referenced_ids(self) -> Set[str]:
        """Collect all IDs referenced by paint servers, clipping/masking/filtering
        attributes, markers, and style-based url(#...) references.

        This routine performs a full sweep of the SVG DOM and extracts every
        referenced ID that appears in attributes such as:

            * clip-path
            * mask
            * filter
            * fill
            * stroke
            * marker-start / marker-mid / marker-end
            * href / xlink:href
            * style="... url(#id) ..."

        It uses `ref_target()` to resolve paint servers and other reference
        attributes, logging each discovered reference. Style attributes are
        parsed manually because they may contain embedded url(#...) tokens
        that do not appear in direct attributes.

        Returns:
            Set[str]:
                A set of all referenced IDs found anywhere in the document.
        """

        referenced = set()

        REF_ATTRS = (
            "clip-path", "mask", "filter",
            "fill", "stroke",
            "marker-start", "marker-mid", "marker-end",
            "href", "{http://www.w3.org/1999/xlink}href"
        )

        for el in self.svg.xpath(".//*[@*]"):
            # Direct attributes
            for attr in REF_ATTRS:
                ref, ref_id = self.ref_target(el, attr)
                
                if not ref is None:
                    self.log(logging.DEBUG, f"[SWEEP] {ref_id} <- {attr} <- {self.node_str(el)}")
                    referenced.add(ref_id)

            # Style attribute
            style = el.get("style")
            if style and "url(#" in style:
                for part in style.split(";"):
                    if "url(#" in part:
                        start = part.find("url(#") + 5
                        end = part.find(")", start)
                        if end > start:
                            referenced.add(part[start:end])

        return referenced

    
    def cleanup_defs(self) -> int:
        """Remove unused <defs> children by repeatedly sweeping for unreferenced IDs.

        This routine performs a multi-pass garbage-collection sweep over the
        document's <defs> section. Because removing one unused definition may
        cause other definitions to become unreferenced, the method repeats the
        sweep until no further deletions occur.

        Logic:
            * Locate the document's <defs> element.
            * Repeatedly:
                - collect all referenced IDs via :meth:`collect_referenced_ids`
                - remove any <defs> child whose ID is not referenced
            * Continue until a full pass removes nothing.

        The method logs each removal and returns the total number of deleted
        nodes.

        Returns:
            int:
                The total number of <defs> children removed across all passes.
        """

        deleted_nodes = 0

        defs = self.svg.find(".//{http://www.w3.org/2000/svg}defs")
        if defs is None:
            return 0

        removed_any = True

        while removed_any:
            referenced = self.collect_referenced_ids()
            removed_any = False

            for child in defs.xpath("./*"):
                cid = child.get("id")
                if not cid:
                    continue

                if cid not in referenced:
                    self.log(logging.DEBUG,
                        f"[DEFS] Removing unused defs child {self.node_str(child)}"
                    )
                    defs.remove(child)
                    removed_any = True
                    deleted_nodes += 1
                else:
                    self.log(logging.DEBUG,
                        f"[DEFS] Keeping defs child id={cid}"
                    )

        return deleted_nodes


    def remove_unreferenced_referenceable(self, node: BaseElement, referenced_ids:Set[str]) -> bool:
        """
        Apply a geometric transform to a supported SVG geometry element.

        This routine dispatches the element to the appropriate shape-specific
        transformer (path, circle, ellipse, rect). Unsupported geometry types
        are left unchanged and reported with a status of 0. After a successful
        transform, the element's `transform` attribute is removed and any
        referenced gradients are updated to reflect the new coordinate system.

        Error handling:
            - If a transform routine raises an exception, the failure is logged
            and the element is returned unchanged.
            - On success, the function returns status 1 together with the updated
            element.

        Returns:
            tuple[int, BaseElement]:
                (status, node) where status is 1 on success and 0 if the element
                was unsupported or an error occurred.
        """

        # Must have an id
        cid = node.get("id")
        if cid is None:
            self.log(logging.DEBUG, f"[SWEEP] NO ID")
            return False

        # Must be a referenceable tag
        tag = self.tag_name(node)
        if tag not in self.REFERENCEABLE_TAGS:
            if self.options.compress_output:
                node.attrib.pop("id", None)

            return False

        # Must NOT be referenced
        if cid in referenced_ids:
            return False

        # Remove it
        parent = node.getparent()
        if parent is not None:
            self.log(logging.DEBUG, f"[SWEEP] Removing {self.node_str(node)}")
            parent.remove(node)
            return True

        return False


    def clean_stroke_attributes(self, node: BaseElement) -> None:
        """
        Remove invisible stroke attributes from an element.

        This routine inspects an element's `stroke` and `stroke-width` attributes
        and deletes all stroke-related presentation attributes when the stroke is
        visually irrelevant. A stroke is considered invisible when:

            - `stroke="none"` is explicitly set, or
            - `stroke-width` parses to a numeric value of 0.

        The function normalizes the stroke-width value, safely handles malformed
        inputs, and removes every attribute listed in `STROKE_ATTRS` when the
        stroke is invisible. Elements without stroke information are left
        unchanged.
        """

        stroke = node.get("stroke")
        stroke_width = node.get("stroke-width")

        zero_width = False
        if stroke_width is not None:
            sw = str(stroke_width).strip()
            try:
                zero_width = float(sw.rstrip("px")) == 0.0
            except ValueError:
                zero_width = False

        stroke_none = (
            stroke is not None
            and str(stroke).strip().lower() == "none"
        )

        if stroke_none or zero_width:
            for attr in self.STROKE_ATTRS:
                node.attrib.pop(attr, None)


    def round_coordinates_on_node(self, node: BaseElement) -> None:
        """
        Round all floating-point coordinate attributes on a single SVG node.

        This routine normalizes numeric precision across path data, geometric
        attributes, and transform attributes by scanning each relevant field and
        rewriting all float literals using the configured rounding precision.
        Supported float formats include standard decimals, fraction-only forms,
        trailing-decimal forms, and scientific notation.

        The function processes:
            - path data (`d`)
            - geometric attributes (x, y, cx, cy, r, rx, ry, width, height,
            stroke-width, x1, y1, x2, y2, fx, fy)
            - transform attributes (`transform`, `gradientTransform`)

        Each attribute is rewritten using `round_floats_in_string()`, ensuring
        consistent numeric formatting throughout the document.
        """

        float_attrs = (
            "x", "y", "cx", "cy", "r", "rx", "ry",
            "width", "height", "stroke-width",
            "x1", "y1", "x2", "y2",
            "fx", "fy",
        )

        digits = self.options.rounding_precision

        # Path data
        if "d" in node.attrib:
            node.set("d", self.round_floats_in_string(node.get("d") or "", digits))

        # Generic float attributes
        for attr in float_attrs:
            if attr in node.attrib:
                node.set(attr, self.round_floats_in_string(node.get(attr) or "", digits))

        # Transform attributes
        if "transform" in node.attrib:
            node.set("transform", self.round_floats_in_string(node.get("transform") or "", digits))

        if "gradientTransform" in node.attrib:
            node.set("gradientTransform", self.round_floats_in_string(node.get("gradientTransform") or "", digits))


    def remove_comments(self, node:BaseElement|None=None) -> None:
        """
        Recursively remove all XML comment nodes from the SVG DOM.

        This routine walks the element tree depth-first and deletes any node
        whose type is `inkex.etree._Comment`. Non-SVG nodes are skipped entirely
        to avoid traversing text nodes, processing instructions, or other
        non-element types. The function also skips `<sodipodi:namedview>` because
        it is metadata rather than drawable content.

        Only real SVG element nodes are recursed into, ensuring safe traversal
        without encountering nodes lacking `.tag` or `.attrib`. The method
        modifies the DOM in place and performs no other cleanup beyond comment
        removal.
        """

        if node is None:
            node = self.svg

        for el in list(node):

            # 1. Remove comment nodes
            if isinstance(el, inkex.etree._Comment):
                node.remove(el)
                continue

            # 2. Skip non-SVG nodes entirely
            if not self.is_svg_node(el):
                continue

            # 3. Skip namedview
            if self.tag_name(el) == "namedview":
                continue

            # 4. Recurse into real SVG elements
            self.remove_comments(el)


    def strip_non_gt7_attributes(self, node: BaseElement) -> None:
        """
        Strip all attributes from a geometry or group element that are not allowed
        in GT7-safe SVG output.

        This routine checks whether the node is a drawable geometry element or a
        <g> group. If not, it returns immediately. For eligible nodes, every
        attribute not listed in `GT7_ATTRS` is removed. This enforces a strict
        attribute whitelist required for Gran Turismo 7 decal compliance and
        eliminates editor-specific metadata, presentation attributes, and
        non-portable SVG features.

        The function modifies the node in place and performs no additional
        validation or structural changes.
        """

        tag = self.tag_name(node)
        if not (self.is_geometry(node) or tag == "g"):
            return

        for attr in list(node.attrib.keys()):
            if attr not in self.GT7_ATTRS:
                node.attrib.pop(attr, None)


    def remove_non_gt7_element(self, node: BaseElement) -> bool:
        """
        Remove elements and attributes that are not permitted in GT7-safe SVG output.

        This routine checks whether the given node is a forbidden SVG element
        such as <script>, <style>, <foreignObject>, multimedia elements, or
        animation elements. Any such node is immediately removed from the DOM.
        It also removes Inkscape- and Sodipodi-namespaced elements, which are
        editor-specific and not valid for GT7 decals.

        If the node itself is allowed, the function still strips any namespaced
        Inkscape/Sodipodi attributes from it. The method returns True when the
        node was deleted and False otherwise.
        """

        forbidden_tags = {
            "script", "style", "foreignObject", "switch",
            "metadata", "desc", "title", "image",
            "iframe", "audio", "video",
            "animate", "animateTransform", "set",
        }

        tag = self.tag_name(node)

        # Forbidden element
        if tag in forbidden_tags:
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
            return True

        # Namespaced editor elements
        if tag.startswith("{http://www.inkscape.org/namespaces/inkscape}") or \
        tag.startswith("{http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd}"):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
            return True

        # Strip namespaced attributes
        for name in list(node.attrib.keys()):
            if name.startswith("{http://www.inkscape.org/namespaces/inkscape}") or \
            name.startswith("{http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd}"):
                del node.attrib[name]

        return False


    def remove_empty_container(self, node: BaseElement) -> bool:
        """
        Remove empty container elements such as <g>, <defs>, <style>, <clipPath>, and
        other structural SVG nodes that serve only as grouping or resource containers.

        A container is considered removable when:
            - it is one of the known container tags eligible for deletion,
            - it has no child nodes,
            - it has no attributes (e.g., id, class, transform).

        The root <svg> element is never removed. If the container meets all criteria,
        it is deleted from its parent and the function returns True. Otherwise, the
        node is left unchanged and the function returns False.
        """


        # Never delete the root <svg>
        if node is self.svg:
            return False

        tag = self.tag_name(node)

        # Containers that may be safely removed when empty
        removable_containers = {
            "g",
            "defs",
            "style",
            "clipPath",
            "mask",
            "pattern",
            "filter",
            "symbol",
            "marker",
            "switch",
        }

        # Only consider known container types
        if tag not in removable_containers:
            return False

        # If the node has children, it is not empty
        if len(node) > 0:
            return False

        # Optional: if the node has attributes, you may want to keep it
        # Remove this check if you want to delete even <g id="foo"></g>
        if len(node.attrib) > 0:
            return False

        # Delete the node
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)
            return True

        return False


    def compress_path_d(self, node:BaseElement) -> bool:
        """
        GT7-safe whitespace compressor for SVG path data.

        This routine rewrites the `d` attribute of a <path> element by removing
        superfluous whitespace while preserving syntactic requirements of the SVG
        path grammar. It eliminates spaces after command letters and collapses
        all other whitespace, except where spacing is mandatory between arc
        flags (0 0, 0 1, 1 0, 1 1). The result is a compact, GT7-compatible path
        string with no semantic changes to the geometry.

        Returns:
            bool:
                True if the node was a <path> and its `d` attribute was rewritten;
                False otherwise.
        """

        if self.tag_name(node) != "path":
            return False

        d = node.get("d", "")

        if not d:
            return False

        # 1. Remove whitespace after command letters (M  → M)
        d = re.sub(self.STRIP_WHITESPACE_AFTER_CMD_LETTERS, r'\1', d)

        # 2. Collapse whitespace everywhere EXCEPT between arc flags
        #    Negative lookbehind: do not remove space after first flag
        #    Negative lookahead: do not remove space before second flag
        d = re.sub(self.STRIP_WHITESPACE_EXCEPT_BETWEEN_ARC_CMD, ' ', d)

        # 3. Trim outer whitespace and replace d attribute
        node.set("d", d.strip())
        
        return True


    def strip_indent_text(self, node:BaseElement) -> None:
        """
        Remove indentation whitespace stored in an element's .text and .tail fields.

        SVG formatting often introduces newline and indentation whitespace that
        lxml represents as text nodes attached to `.text` (content before the
        first child) and `.tail` (content after the element, before the next
        sibling). These whitespace nodes are not real children and cannot be
        removed via DOM operations.

        This routine clears `.text` and `.tail` when they contain only
        whitespace, ensuring a clean, compact DOM without formatting artifacts.
        """

        # Remove whitespace-only .text
        if node.text and not node.text.strip():
            node.text = None

        # Remove whitespace-only .tail
        if node.tail and not node.tail.strip():
            node.tail = None


    def add_indent_text(self, node, level: int) -> None:
        """
        Insert pretty-print indentation whitespace for a single SVG node.

        This routine adds newline-based indentation to `.text` and `.tail` fields
        to produce human-readable XML formatting. It does not recurse into child
        nodes; instead, it relies on the caller to supply the correct indentation
        level.

        Behavior:
            - For elements with children:
                - `.text` receives indentation before the first child.
                - `.tail` of the last child receives indentation after the element.
            - For leaf elements:
                - `.tail` receives indentation only.

        Whitespace is added only when the existing `.text` or `.tail` fields are
        missing or contain only whitespace, ensuring that meaningful text content
        is never overwritten.
        """

        self.log(logging.DEBUG, f"indent={level} {self.node_str(node)}")

        if not hasattr(node, "tag"):
            return

        indent = "\n" + "  " * level

        # If node has children, indent before first child
        if len(node):
            if node.text is None or not node.text.strip():
                node.text = indent + "  "

            # indent after last child only
            last = node[-1]
            if last.tail is None or not last.tail.strip():
                last.tail = indent
        else:
            # leaf node: indent tail only
            if node.tail is None or not node.tail.strip():
                node.tail = indent


    def sweep_svg_tree(self):
        """
        Perform a full sweep-and-clean pass over the SVG tree.

        This routine orchestrates the complete cleanup pipeline by combining
        defs-garbage-collection, presentation-attribute grouping, and repeated
        structural pruning. The process runs in three phases:

            1. Remove unused <defs> entries via cleanup_defs(), including
            multi-pass elimination of gradients, clipPaths, markers, and
            other referenceable resources.

            2. Group shapes by common presentation attributes to reduce
            redundancy and simplify the DOM structure.

            3. Repeatedly invoke clean_svg_tree() until no further nodes are
            deleted. This stabilizes the tree by removing empty containers,
            forbidden elements, unreferenced resources, and non-GT7-safe
            constructs.

        The method logs intermediate states and returns no value. A summary of
        the total number of deleted nodes is emitted at INFO level.
        """

        totally_deleted_nodes = self.cleanup_defs()
        self.group_by_common_presentation_attributes()
        self.log_svg(header= "AFTER group_by_common_presentation_attributes")

        loop = True

        while loop:
            deleted_nodes = self.clean_svg_tree()
            loop = deleted_nodes > 0
            totally_deleted_nodes += deleted_nodes

        self.log(logging.INFO, f"Sweeped {totally_deleted_nodes} nodes from SVG tree")


    def remove_empty_text_node(self, node) -> bool:
        """
        Remove whitespace-only text nodes created by pretty-printed SVG formatting.

        This routine detects non-element nodes (text, tail text, comments, and
        other non-tag content) by checking for the absence of a `.tag` attribute.
        If such a node contains only whitespace, it is removed from its parent.
        This eliminates indentation artifacts introduced by XML pretty-printing
        and ensures that structural cleanup routines can correctly identify
        empty containers without interference from stray text nodes.

        Returns:
            bool:
                True if a whitespace-only text node was removed, False otherwise.
        """

        # If it's not an element, it's a text node or comment
        if not hasattr(node, "tag"):
            # Convert to string and check if empty/whitespace
            if not str(node).strip():
                parent = node.getparent()
                if parent is not None:
                    parent.remove(node)
                return True
        return False


    def clean_svg_tree(self, node:BaseElement|None=None, referenced_ids:Set[str]|None=None, level:int=0) -> int:
        """
        Perform a post-order cleanup pass on the SVG tree, removing invalid,
        forbidden, empty, and unreferenced nodes while normalizing attributes and
        formatting.

        This routine recursively traverses the SVG DOM, processing children
        before their parent to ensure that container-emptiness checks and
        referenceability checks operate on a fully updated subtree. For each
        node, the following cleanup steps are applied in order:

            1. remove_empty_text_node():
                Deletes whitespace-only text/tail nodes that interfere with
                structural emptiness detection.

            2. remove_non_gt7_element():
                Removes forbidden SVG elements (script, style, foreignObject,
                animation, multimedia, editor-namespaced nodes) and strips
                Inkscape/Sodipodi namespaced attributes.

            3. remove_empty_container():
                Deletes empty structural containers (<g>, <defs>, <clipPath>,
                <mask>, <pattern>, <filter>, <symbol>, <marker>, <switch>) when
                they have no children and no attributes.

            4. remove_unreferenced_referenceable():
                Removes referenceable nodes (gradients, clipPaths, markers,
                filters, etc.) whose IDs are not present in the current set of
                referenced IDs.

        If none of the deletion rules apply, the node is retained and normalized:

            - strip_non_gt7_attributes() removes disallowed attributes.
            - clean_stroke_attributes() removes invisible stroke definitions.
            - round_coordinates_on_node() applies numeric rounding.
            - strip_indent_text() or add_indent_text() adjusts whitespace
            depending on whether output compression is enabled.
            - compress_path_d() rewrites path data for GT7-safe compactness.

        The function returns the number of nodes deleted during this invocation.
        It is typically called repeatedly until no further deletions occur,
        ensuring a stable, fully cleaned SVG tree.
        """

        deleted_nodes = 0
        
        if node is None:
            node = self.svg

        if referenced_ids is None:
            referenced_ids = self.collect_referenced_ids()

        # Postorder traversal: recurse children first, then the node itself

        for child in list(node):
            deleted_nodes += self.clean_svg_tree(child, referenced_ids, level+1)

        # Process node itself after children have been processed

        deleted = False

        deleted = self.remove_empty_text_node(node)

        if not deleted:
            deleted = self.remove_non_gt7_element(node)

        if not deleted:
            deleted = self.remove_empty_container(node)

        if not deleted:
            deleted = self.remove_unreferenced_referenceable(node, referenced_ids)

        if deleted:
            deleted_nodes += 1

        else:
            self.strip_non_gt7_attributes(node)
            self.clean_stroke_attributes(node)
            self.round_coordinates_on_node(node)

            if self.options.compress_output:
                self.strip_indent_text(node)
                self.compress_path_d(node)
            else:
                self.add_indent_text(node, level)
                

        return deleted_nodes


    def group_by_common_presentation_attributes(self, node:BaseElement|None=None) -> None:
        """
        Group consecutive sibling elements that share identical presentation
        attributes into a single <g> wrapper, reducing redundancy and improving
        structural clarity.

        This routine performs a post-order traversal so that child groups are
        formed before their parents are examined. For each element, it scans its
        immediate children and identifies runs of consecutive siblings whose
        presentation attributes (fill, stroke, opacity, etc.) match exactly. When
        a run contains two or more elements, they are wrapped in a new <g> group
        that carries the shared attributes. The individual elements have those
        attributes removed so that the wrapper becomes the single source of
        presentation styling.

        Gradient and pattern elements are excluded from grouping to avoid
        altering resource definitions. The method updates the DOM in place and
        does not return a value.
        """

        if node is None:
            node = self.svg

        tag = self.tag_name(node)
        if tag in ("linearGradient", "radialGradient", "meshgradient", "pattern"):
            return

        # Recurse first
        for child in list(node):
            self.group_by_common_presentation_attributes(child)

        children = list(node)
        if len(children) < 2:
            return

        i = 0
        while i < len(children):
            base = children[i]
            if not isinstance(base.tag, str):
                i += 1
                continue

            # Extract presentation attributes from base
            base_attrs = {
                k: v for k, v in base.attrib.items()
                if k in self.PRESENTATION_ATTRS
            }
            if not base_attrs:
                i += 1
                continue

            run = [base]
            j = i + 1

            # Scan forward for siblings with identical presentation attributes
            while j < len(children):
                other = children[j]
                if not isinstance(other.tag, str):
                    break

                other_attrs = {
                    k: v for k, v in other.attrib.items()
                    if k in self.PRESENTATION_ATTRS
                }

                if other_attrs != base_attrs:
                    break

                run.append(other)
                j += 1

            # Only group if at least two siblings match
            if len(run) < 2:
                i += 1
                continue

            parent = node
            wrapper = inkex.Group()

            parent_index = parent.index(base)
            self.add_node(wrapper, parent, parent_index)

            # Move children into wrapper and strip their presentation attributes
            for el in run:
                parent.remove(el)
                self.add_node(el, wrapper)

                for k in base_attrs.keys():
                    el.attrib.pop(k, None)

            # Apply shared attributes to wrapper
            for k, v in base_attrs.items():
                wrapper.set(k, v)

            # Refresh children list and continue scanning
            children = list(node)
            i = parent_index + 1


    # region ---- Mesh Gradients ----

    def resolve_meshgradient_chain(self, mg:MeshGradient) -> Stop:
        """
        Resolve a chain of meshgradient references into a single flattened
        gradient definition.

        Meshgradients may inherit attributes, stops, and coordinate data from
        other meshgradient elements via xlink:href. This routine follows that
        reference chain upward, cloning the initial gradient and merging any
        missing attributes from each ancestor. If the starting gradient has no
        own <stop> elements, all stops from the parent are copied. The process
        continues until a gradient without a parent reference is reached.

        The result is a standalone meshgradient containing all inherited
        attributes and stops, suitable for evaluation or conversion without
        further dependency resolution.
        """

        ns = {
            "svg": "http://www.w3.org/2000/svg",
            "xlink": "http://www.w3.org/1999/xlink"
        }

        # Clone starting gradient
        merged = copy.deepcopy(mg)

        # Walk chain upward
        current = mg
        while True:
            parent, _ = self.ref_target(current)
            if parent is None:
                break

            parent = parent[0]

            # --- inherit attributes ---
            for attr, val in parent.attrib.items():
                if attr not in merged.attrib:
                    merged.set(attr, val)

            # --- inherit stops ---
            parent_stops = parent.xpath(".//svg:stop", namespaces=ns)
            merged_stops = merged.xpath(".//svg:stop", namespaces=ns)

            if not merged_stops:
                # child has no stops → inherit all
                for s in parent_stops:
                    merged.append(copy.deepcopy(s))

            # continue walking up
            current = parent

        return merged


    def rgb_to_stop_color(self, rgba: tuple[float, float, float, float]) -> tuple[str, str]:
        """
        Convert an RGBA tuple into two SVG-ready color components.

        This helper takes (r, g, b, a) values where the channels may be expressed
        either in 0-255 integer form or 0-1 normalized form. It returns:

            1) A GT7-safe RGB hex string (#RRGGBB) without an alpha channel.
            2) A stop-opacity string with alpha normalized to the 0-1 range.

        Alpha values greater than 1.0 are interpreted as 0-255 inputs and are
        normalized accordingly. The function performs no clamping and assumes
        the caller provides valid channel ranges.
        """

        r = int(rgba[0])
        g = int(rgba[1])
        b = int(rgba[2])
        a = rgba[3]

        # Normalize alpha if it's in 0-255 range
        if a > 1.0:
            a = a / 255.0

        # Hex color without alpha (GT7-safe, Inkscape-safe)
        hex_rgb = self.rgba_to_hex(r, g, b)

        # Alpha as stop-opacity (CSS/SVG spec)
        stop_opacity = f"{a:.6f}"

        return hex_rgb, stop_opacity


    def make_stop(self, offset: float, rgb: tuple[float, float, float, float]) -> Stop:
        """
        Create a GT7-safe <stop> element from an RGBA color and an offset.

        This routine converts an (r, g, b, a) tuple into a pair of SVG-ready
        components using rgb_to_stop_color(): a #RRGGBB hex string and a
        normalized stop-opacity value. The resulting <stop> element always
        includes an offset and a stop-color attribute. A stop-opacity attribute
        is added only when alpha stripping is disabled, ensuring compatibility
        with GT7 decal requirements.

        The function returns a fully constructed inkex.elements.Stop node.
        """

        color, opacity = self.rgb_to_stop_color(rgb)

        stop = inkex.elements.Stop()  # type: ignore
        stop.set("offset", str(offset))
        stop.set("stop-color", color)
        if not self.options.strip_alpha:
            stop.set("stop-opacity", opacity)

        return stop


    def centroid_of_path(self, pts:list[tuple[float,float]]) -> tuple[float, float]:
        """
        Compute the centroid of a polyline or polygon represented as a list of
        (x, y) coordinate pairs.

        The centroid is calculated as the arithmetic mean of all x-coordinates
        and all y-coordinates. This yields the geometric center for uniformly
        weighted points and is appropriate for path vertex averaging, midpoint
        estimation, and meshgradient patch sampling.

        The function assumes the list contains at least one point and returns a
        2-tuple (cx, cy).
        """

        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (sum(xs)/len(xs), sum(ys)/len(ys))

    
    def build_clipped_gradient(
        self,
        path_elem: PathElement,
        evaluator: "MeshGradientEvaluator"
    ) -> str:
        """
        Construct a userSpaceOnUse linearGradient whose axis is defined by the
        pair of path vertices exhibiting the maximum RGB color difference under
        a meshgradient evaluator.

        The function extracts all M/L vertices from the path, removes duplicate
        points with nanometer-scale tolerance, and chooses an axis as follows:

            - If fewer than two unique vertices exist, fall back to a trivial
            axis at the centroid.
            - Otherwise, evaluate meshgradient colors at each unique vertex and
            search all vertex pairs for the maximum squared RGB distance.
            The pair with the largest color contrast becomes the gradient axis.

        Endpoint colors are sampled via evaluator.color_at_xy(), and a
        GT7-compatible <linearGradient> is created with those two stops. The
        gradient is inserted into <defs>, and its generated ID is returned.

        This produces a clipped gradient aligned with the strongest color
        variation across the clipped geometry, suitable for meshgradient
        approximation or fallback rendering.
        """

        pts: list[tuple[float, float]] = []

        for cmd, params in path_elem.path.to_arrays():
            if cmd in ("M", "L"):
                pts.append((float(params[0]), float(params[1])))

        # remove duplicate points from closed polygons
        unique_pts: list[tuple[float, float]] = []
        seen: set[tuple[int, int]] = set()

        for x, y in pts:
            key = (round(x * 1e9), round(y * 1e9))
            if key not in seen:
                seen.add(key)
                unique_pts.append((x, y))

        if len(unique_pts) < 2:
            cx, cy = self.centroid_of_path(pts or [(0.0, 0.0)])
            p0 = (cx, cy)
            p1 = (cx + 1.0, cy)

        else:

            best_p0 = unique_pts[0]
            best_p1 = unique_pts[1]
            best_score = -1.0

            #
            # Find vertex pair with maximum colour difference.
            #
            vertex_colors = {
                p: evaluator.color_at_point(p[0], p[1])
                for p in unique_pts
            }

            for i in range(len(unique_pts)):
                for j in range(i + 1, len(unique_pts)):

                    c0 = vertex_colors[unique_pts[i]]
                    c1 = vertex_colors[unique_pts[j]]

                    dr = c0[0] - c1[0]
                    dg = c0[1] - c1[1]
                    db = c0[2] - c1[2]

                    # RGB distance
                    score = dr * dr + dg * dg + db * db

                    if score > best_score:
                        best_score = score
                        best_p0 = unique_pts[i]
                        best_p1 = unique_pts[j]

            p0 = best_p0
            p1 = best_p1

        #
        # Sample endpoint colours.
        #
        c0 = evaluator.color_at_point(*p0)
        c1 = evaluator.color_at_point(*p1)

        self.log(
            logging.DEBUG,
            f"gradient axis={p0}->{p1}, colors={c0}->{c1}"
        )

        lg = inkex.LinearGradient()
        lg.set("gradientUnits", "userSpaceOnUse")

        lg.set("x1", str(p0[0]))
        lg.set("y1", str(p0[1]))
        lg.set("x2", str(p1[0]))
        lg.set("y2", str(p1[1]))

        lg.add(self.make_stop(0.0, c0))
        lg.add(self.make_stop(1.0, c1))

        defs = self.ensure_defs()
        self.add_node(lg, defs)

        gid = lg.get("id")
        assert gid is not None

        return gid

    def colorize_clipped_triangles(self, shape:BaseElement, gradient:MeshGradient, triangles:list[PathElement]) -> None:
        """
        Apply clipped-triangle gradient coloring by generating a local linearGradient
        for each triangle and assigning it to the specified presentation attribute.

        For every triangle in the list, a MeshGradientEvaluator is constructed for
        the parent shape and the source meshgradient. Each triangle is then passed
        to build_clipped_gradient(), which determines a gradient axis based on the
        triangle's vertex colors and creates a GT7-safe <linearGradient> in <defs>.
        The returned gradient ID is converted into a url(#id) reference and applied
        to the triangle via the given attribute (typically 'fill' or 'stroke').

        This produces per-triangle fallback gradients that approximate the original
        meshgradient when clipped geometry is decomposed into triangular patches.
        """

        evaluator = MeshGradientEvaluator(gradient, shape, self)

        for t in triangles:
            gid = self.build_clipped_gradient(t, evaluator)
            t.set("fill", self.node_or_id_to_url(gid))


    def replace_meshgradient_on_group(self, group:Group, attr:str, mg:MeshGradient) -> None:
        """
        Replace a meshgradient applied on a <g> group by propagating a cloned
        gradient to each geometry element in the subtree.

        The group's own presentation attribute (fill or stroke) is removed so
        that it no longer references the meshgradient directly. Every geometry
        element reachable through iter_geometry_subtree() receives its own
        independent clone of the meshgradient, and replace_meshgradient() is
        invoked to convert that clone into a GT7-safe fallback (typically a
        linearGradient or per-triangle clipped gradient).

        This ensures that meshgradient usage is eliminated at the group level
        and replaced with per-shape gradients suitable for GT7-compatible export.
        """

        group.attrib.pop(attr, None)

        for shape, _ in self.iter_geometry_subtree(group, attr=attr, pop_attr=True):
            mg_clone = copy.deepcopy(mg)
            self.replace_meshgradient(shape, attr, mg_clone)


    def derive_mesh_grid(self, n_div:int, width:float|None=None, height:float|None=None) -> tuple[int, int]:
        """
        Derive a power-of-two grid (rows, cols) whose total cell count matches
        2^(n_div-1), ensuring that the downstream triangle count
        2 * rows * cols equals 2^n_div.

        The grid is chosen to be as balanced as possible rather than forcing a
        fixed 2-row layout. The routine computes a power-of-two row count and
        derives the column count from the required total number of cells. When
        optional width/height hints are provided, the grid is oriented so that
        the longer dimension receives the larger subdivision count.

        Returns a (rows, cols) tuple suitable for deterministic triangulation of
        a bounding box into exactly 2^n_div triangles.
        """

        n_div = max(0, int(n_div))
        cells = max(1, 2 ** max(0, n_div - 1))

        if cells == 1:
            return 1, 1

        rows = 1 << ((n_div - 1) // 2)
        cols = max(1, cells // rows)

        if width is not None and height is not None and width > 0 and height > 0:
            if height > width and rows < cols:
                rows, cols = cols, rows
            elif width > height and cols < rows:
                rows, cols = cols, rows

        return rows, cols


    def replace_meshgradient(self, shape:BaseElement, attr:str, mg:MeshGradient) -> List[PathElement] | None:
        """
        Replace a meshgradient on a geometry element. For strokes a single dominent color
        is set, while filling is replaced by triangulating the shape into a power-of-two 
        triangle grid, clipping each triangle to the shape, and assigning a per-triangle fallback
        linearGradient derived from local meshgradient color variation.

        Returns the list of triangles that replaced the shape or None if the shape was neither removed
        nor replaced.
        """
        if attr == "stroke":
            self.replace_stroke_meshgradient(shape, mg)
            return None
        else:
            return self.replace_fill_meshgradient(shape, mg)


    def replace_stroke_meshgradient(self, shape:BaseElement, mg:MeshGradient) -> None:
        """
        Replace a MeshGradient stroke with a single color - the dominant colot of the
        MeshGradient.
        """
        eval = MeshGradientEvaluator(mg, shape, self)

        rgba = eval.dominant_color()
        r = int(rgba[0])
        g = int(rgba[1])
        b = int(rgba[2])

        color = self.rgba_to_hex(r, g, b)
        shape.set("stroke", color)


    def replace_fill_meshgradient(self, shape:BaseElement, mg:MeshGradient) -> List[PathElement]:
        """
        Replace a meshgradient on a geometry element by triangulating the shape into a power-of-two 
        triangle grid, clipping each triangle to the shape, and assigning a per-triangle fallback
        linearGradient derived from local meshgradient color variation.

        The procedure consists of:

            - Resolving chained meshgradient inheritance so the evaluator sees a
            fully flattened gradient definition.

            - Computing the shape's bounding box and deriving a (rows, cols)
            subdivision whose total triangle count is exactly 2^n_div, using
            derive_mesh_grid() to balance the grid according to the shape's
            aspect ratio.

            - Generating two triangles per grid cell to tessellate the bounding
            box.

            - Clipping all triangles to the shape via path_intersection(), which
            yields the actual clipped geometry patches.

            - For each clipped triangle, constructing a local linearGradient
            whose axis is defined by the pair of triangle vertices with the
            largest RGB color difference under the meshgradient evaluator.
            These gradients are inserted into <defs> and applied to the
            triangle via the given presentation attribute.

            - Inserting the clipped triangles into the DOM in paint-order-aware
            position, disabling inherited stroke, and making the original
            shape transparent or removing it if it becomes invisible.

        The function returns the list of clipped triangle PathElements, each
        already colorized with a GT7-safe fallback gradient.
        """

        n_div = self.options.mesh_divisions
        self.log(logging.DEBUG, f"Mesh divisions={n_div}")

        # --- Step 0: resolve chained attributes (still useful for later coloring) ---
        mg = self.resolve_meshgradient_chain(mg)

        # --- Step 1: get bounding box of the shape in user units ---
        bbox = shape.bounding_box()  # inkex provides this on PathElement/ShapeElement
        x0 = bbox.left
        y0 = bbox.top
        x1 = bbox.right
        y1 = bbox.bottom

        # --- Step 2: derive grid from division factor n_div ---
        # Total triangles must be 2^n_div, so cell count is 2^(n_div-1).
        # Use a balanced power-of-two split instead of the fixed two-row layout.
        rows, cols = self.derive_mesh_grid(n_div, x1 - x0, y1 - y0)
        dx = (x1 - x0) / cols
        dy = (y1 - y0) / rows

        # --- Step 3: build triangles covering the bounding box ---
        triangles = []
        for i in range(cols):
            for j in range(rows):
                # Round coordinates to a fixed precision, overlap by epsilon
                epsilon = 0 #TODO determine right number for epsilon, aka is this needed for GT7
                xL = round(x0 + i * dx - epsilon, self.options.rounding_precision)
                xR = round(x0 + (i + 1) * dx + epsilon, self.options.rounding_precision)
                yB = round(y0 + j * dy - epsilon, self.options.rounding_precision)
                yT = round(y0 + (j + 1) * dy + epsilon, self.options.rounding_precision)

                # Triangle A: bottom-left, bottom-right, top-right
                d_a = f"M {xL},{yB} L {xR},{yB} L {xR},{yT} Z"
                t_a = inkex.PathElement()
                t_a.set("d", d_a)
                triangles.append(t_a)

                # Triangle B: bottom-left, top-right, top-left
                d_b = f"M {xL},{yB} L {xR},{yT} L {xL},{yT} Z"
                t_b = inkex.PathElement()
                t_b.set("d", d_b)
                triangles.append(t_b)

        self.log(logging.DEBUG, f"Generated {len(triangles)} triangles for n_div={n_div}")

        # --- Step 4: clip all triangles to the shape ---
        clipped = self.path_intersection(shape, triangles)
        self.log(logging.DEBUG, f"{len(clipped)} clipped triangles after intersection")

        # --- Step 5: basic styling (black fill / white stroke for inspection) ---
        self.colorize_clipped_triangles(shape, mg, clipped)

        # Add clipped triangles to DOM
        fill_pos, stroke_pos, _ = self.parse_paint_order(shape)
        parent, idx = self.parent_of(shape)

        if fill_pos > stroke_pos:
            idx += 1

        for p in clipped:
            p.set("stroke", "none")  # override group stroke
            self.add_node(p, parent, idx)

        # Make shape transparent
        shape.set("fill", "none")
        if not self.is_geometry_visible(shape):
            self.remove_node(shape)

        return clipped

    # endregion

    # region --- Gradients ---

    def parse_and_sort_stops(self, grad:LinearGradient|RadialGradient) ->list[tuple[float,Stop]]:
        """
        Parse all <stop> elements in a gradient, normalize their offset values,
        and return them sorted by numeric offset.

        The routine uses the gradient's own SVG namespace to locate <stop>
        children (direct or nested). Each stop's “offset” attribute is parsed
        robustly: percentage values are converted to 0-1 floats, plain numeric
        values are interpreted directly, and malformed offsets fall back to 0.0.
        The result is a list of (offset, Stop) tuples sorted in ascending order,
        providing a clean, normalized stop sequence for gradient processing.
        """

        # Use the gradient's own namespace instead of hard-coding SVG ns
        svg_ns = self.svg.nsmap.get(None, "http://www.w3.org/2000/svg")

        # Find all <stop> children (direct or nested)
        stops = grad.iterfind(f".//{{{svg_ns}}}stop")

        parsed = []
        for s in stops:
            off = (s.get("offset") or "0").strip()

            # Parse offset: %, float, malformed → fallback to 0
            try:
                if off.endswith("%"):
                    val = float(off[:-1]) / 100.0
                else:
                    val = float(off)
            except ValueError:
                val = 0.0

            parsed.append((val, s))

        # Sort by numeric offset
        parsed.sort(key=lambda x: x[0])
        return parsed


    def stop_is_transparent(self, stop) -> bool:
        """
        Determine whether a <stop> element is effectively transparent.

        A stop is considered transparent under the following conditions:

            - It has an explicit stop-opacity of 0.
            - It lacks a stop-color attribute, which SVG interprets as transparent.
            - Its stop-color is the named value “transparent”.
            - Its stop-color encodes an alpha channel (hex RGBA, rgba(), hsla(), etc.)
            and strip_alpha_from_stop(..., replace=False) reports that alpha was
            present and non-opaque.

        The function performs no mutation unless strip_alpha_from_stop() is invoked
        with replace=True elsewhere; here it is strictly a transparency test.
        Returns True if the stop contributes no visible color.
        """

        col = stop.get("stop-color")
        op  = stop.get("stop-opacity")

        # Explicit opacity=0
        if op is not None and float(op) == 0:
            return True

        # No color → transparent by SVG spec
        if not col:
            return True

        # Named colors
        if col.lower() == "transparent":
            return True

        return self.strip_alpha_from_stop(stop, replace=False)


    # --- Helper: check gradient transparency ---
    def gradient_is_transparent(self, grad: LinearGradient|RadialGradient|MeshGradient) -> bool:
        """
        Determine whether a gradient is effectively transparent by inspecting its
        stop sequence and resolving chained references when necessary.

        A gradient is considered transparent if:

            - The gradient reference is invalid (None).
            - It contains no <stop> elements; in this case the routine attempts to
            follow xlink:href via ref_target() and recursively re-evaluate the
            referenced gradient.
            - All of its stops are individually transparent according to
            stop_is_transparent(), which checks explicit stop-opacity=0,
            missing stop-color, the named value “transparent”, or any encoded
            alpha channel in the stop-color.

        This function provides a robust visibility test for linear, radial, and
        mesh gradients, ensuring that empty or fully transparent gradients are
        treated as invisible during GT7-safe cleanup and geometry pruning.
        """

        if grad is None:
            return True  # invalid reference → invisible

        stops = grad.findall(".//{http://www.w3.org/2000/svg}stop")
        if not stops:
            grad, _ = self.ref_target(grad, tag_name=self.tag_name(grad))
            if grad is None:
                return True  # invalid reference → invisible
            else:
                return self.gradient_is_transparent(grad)  # recurse    

        # All stops transparent?
        return all(self.stop_is_transparent(s) for s in stops)


    def strip_alpha_from_stop(self, stop: Stop, replace:bool=True) -> bool:
        """
        Strip alpha information from a <stop> element and report whether the stop
        is fully transparent after combining color-encoded alpha with stop-opacity.

        The routine parses the stop-color via parse_color(), which returns a
        GT7-safe RGB string plus an 8-bit alpha channel. If alpha stripping is
        enabled, the stop is treated as fully opaque (opacity = 1.0). Otherwise,
        the effective opacity is computed as:

            effective_opacity = stop-opacity * (alpha / 255)

        When replace=True, the stop-color is rewritten without its alpha channel
        and stop-opacity is updated to the computed effective value. No mutation
        occurs when replace=False.

        The function returns True only when the resulting effective opacity is
        exactly 0.0, meaning the stop contributes no visible color.
        """

        str_color = stop.get("stop-color")
        if str_color is not None:
            color, alpha = self.parse_color(str_color)

        if self.options.strip_alpha:
            opacity = 1.0
        else:
            str_opacity = stop.get("stop-opacity") or "1.0"
            opacity = float(str_opacity)
            opacity = opacity * alpha / 255

        # alpha was present → strip it
        if replace:
            if color is not None:
                stop.set("stop-color", color)
                
            stop.set("stop-opacity", opacity)
            self.log(logging.DEBUG, f"Retained opacity = {opacity}")

        return opacity == 0.0


    def reduce_to_first_and_last_stop(self, grad:LinearGradient|RadialGradient, parsed:list[tuple[float,Stop]]) -> tuple[Stop,Stop]:
        """
        Reduce a gradient's stop list to exactly two stops—first and last—while
        normalizing offsets, stripping alpha if enabled, and removing all
        intermediate stops.

        The routine expects a parsed stop list of (offset, Stop) tuples already
        sorted by offset. If the gradient contains only one stop, that stop is
        duplicated so the gradient remains valid for GT7-safe export. The first
        and last stops are forced to offsets 0 and 1, respectively, and
        strip_alpha_from_stop() is applied to each. Any stops between them are
        removed from the DOM.

        Returns a (first_stop, last_stop) tuple representing the normalized
        two-stop gradient.
        """

        # If only one stop exists → duplicate it
        if len(parsed) == 1:
            orig = parsed[0][1]
            clone = copy.deepcopy(orig)
            self.add_node(clone, grad)
            parsed = [(0.0, orig), (1.0, clone)]

        # First and last stop elements
        first = parsed[0][1]
        last  = parsed[-1][1]

        # Normalize offsets
        first.set("offset", "0")
        last.set("offset", "1")

        # Strip alpha if enabled
        self.strip_alpha_from_stop(first)
        self.strip_alpha_from_stop(last)

        # Remove all intermediate stops
        for _, s in parsed[1:-1]:
            if s not in (first, last):
                self.remove_node(s, grad)

        return first, last
    

    def normalize_gradient_stops_and_colors(self, grad:LinearGradient|RadialGradient) -> None:
        """
        Normalize a gradient's stop list by reducing it to a canonical two-stop
        form and stripping alpha channels according to the global configuration.

        The routine first parses and sorts all <stop> elements using
        parse_and_sort_stops(). If no stops exist, the gradient is left unchanged.
        Otherwise, reduce_to_first_and_last_stop() is applied, which enforces
        offsets 0 and 1, removes intermediate stops, and rewrites stop-color /
        stop-opacity when alpha stripping is enabled.

        A debug log entry records the gradient's ID and whether alpha stripping
        was active. No value is returned; the gradient is modified in place.
        """

        # Parse + sort stops (svg-API safe)
        parsed = self.parse_and_sort_stops(grad)
        if not parsed:
            return

        # Reduce to first + last stop (alpha stripping controlled by global flag)
        self.reduce_to_first_and_last_stop(grad, parsed)

        # Logging
        gid = grad.get("id", "")
        self.log(logging.DEBUG,
                 f"Normalized gradient stops for id={gid} "
                 f"(first+last only, alpha stripped={self.options.strip_alpha})")


    def normalize_gradient_units(self, grad:LinearGradient|RadialGradient, shape:BaseElement) -> None:
        """
        Normalize a gradient's coordinate system by converting objectBoundingBox
        units into userSpaceOnUse and baking the required bounding-box transform
        directly into the gradient's coordinate attributes.

        If gradientUnits is already userSpaceOnUse, no action is taken. Otherwise,
        the routine computes a bounding-box normalization transform for the given
        shape via bbox_transform(), which encapsulates the shape's full transform
        chain and its user-space bounding box. This transform is applied to all
        gradient coordinate attributes (x1, y1, x2, y2, cx, cy, r, fx, fy, etc.)
        using apply_transform_to_gradient(), after which gradientUnits is set to
        "userSpaceOnUse".

        The result is a gradient whose coordinates are fully expressed in user
        space, with any prior gradientTransform folded into the baked-in values.
        This ensures GT7-safe, Inkscape-safe behavior and eliminates reliance on
        objectBoundingBox normalization at render time.
        """

        # 0. Already userSpaceOnUse → nothing to do
        if grad.get("gradientUnits") == "userSpaceOnUse":
            return

        gid = grad.get("id", "")
        self.log(logging.DEBUG,
                 f"Converting gradient id={gid} from objectBoundingBox → userSpaceOnUse")

        # 1. Compute bounding box in user space
        T_bbox = self.bbox_transform(shape)

        self.log(logging.DEBUG,f"T_bbox={T_bbox}")

        # 5. Apply transform to gradient coordinates
        self.apply_transform_to_gradient(grad, T_bbox)

        # 7. Force userSpaceOnUse
        grad.set("gradientUnits", "userSpaceOnUse") 

        self.log(logging.DEBUG,
                 f"{self.node_str(grad)} converted to userSpaceOnUse")


    def clone_gradient(self, grad:LinearGradient|RadialGradient|MeshGradient) -> LinearGradient | RadialGradient | MeshGradient:
        """
        Clone a gradient element (linear, radial, or mesh) into a fresh,
        ID-stripped copy while preserving all non-ID attributes and stop
        structure.

        The routine normalizes the gradient's tag name to handle Inkscape's
        meshGradient/meshgradient case variations, dispatches to the correct
        inkex element class, and copies all attributes except “id”. Every
        <stop> descendant is duplicated into a new Stop node with its attributes
        preserved (again excluding “id”). Finally, all IDs in the cloned
        gradient's subtree are removed to ensure GT7-safe, collision-free
        insertion into <defs>.

        The returned gradient is a structurally faithful clone suitable for
        further normalization (units, stops, transforms) without mutating the
        original definition.
        """

        # Determine gradient type using svg-API tag resolution.
        # Inkscape saves mesh gradients with either meshGradient or meshgradient,
        # so normalize case before dispatching.
        tag = self.tag_name(grad).lower()

        if tag == "lineargradient":
            new_grad = inkex.elements.LinearGradient() # type: ignore
        elif tag == "radialgradient":
            new_grad = inkex.elements.RadialGradient() # type: ignore
        elif tag == "meshgradient":
            new_grad = inkex.elements.MeshGradient() # type: ignore
        else:
            raise TypeError(f"Unsupported gradient tag for clone: {self.tag_name(grad)!r}")

        # ---------------------------------------------------------
        # Copy attributes except id
        # ---------------------------------------------------------
        for k, v in grad.attrib.items():
            if k != "id":
                new_grad.set(k, v)

        # ---------------------------------------------------------
        # Clone stops (no IDs)
        # ---------------------------------------------------------
        svg_ns = self.svg.nsmap.get(None, "http://www.w3.org/2000/svg")

        for stop in grad.iterfind(f".//{{{svg_ns}}}stop"):
            new_stop = inkex.elements.Stop() # type: ignore
            for k, v in stop.attrib.items():
                if k != "id":
                    new_stop.set(k, v)
            new_grad.add(new_stop)

        # ---------------------------------------------------------
        # Strip IDs from gradient + all descendants
        # ---------------------------------------------------------
        new_grad.attrib.pop("id", None)
        for el in new_grad.iter():
            el.attrib.pop("id", None)

        return new_grad


    def needs_linear_defaults(self, grad:LinearGradient)-> bool:
        """
        Return True when a linearGradient has no explicit coordinate attributes
        (x1, y1, x2, y2), meaning it still relies on SVG's objectBoundingBox
        defaults.

        This predicate is used before applying fallback coordinate assignment or
        before converting the gradient to userSpaceOnUse, allowing the caller to
        distinguish between gradients that define their own geometry and those
        that require default coordinate injection.
        """

        return (
            grad.get("x1") is None and
            grad.get("y1") is None and
            grad.get("x2") is None and
            grad.get("y2") is None
        )


    def apply_linear_defaults(self, grad:LinearGradient, bbox:tuple[float,float,float,float]) -> None:
        """
        Apply SVG default coordinates for a linearGradient when no explicit
        (x1, y1, x2, y2) are provided, converting the gradient into a fully
        user-space definition.

        The function first verifies that the element is a linearGradient and
        that all four coordinate attributes are missing, meaning the gradient
        still relies on objectBoundingBox defaults. In that case, it switches
        gradientUnits to userSpaceOnUse and assigns pixel-space coordinates
        derived from the provided bounding box tuple (bx, by, bw, bh):

            x1 = bx
            y1 = by
            x2 = bx + bw
            y2 = by

        These values reproduce SVG's objectBoundingBox default axis but in
        absolute user-space form, ensuring deterministic behavior once the
        gradient is normalized and any gradientTransform is removed.
        """

        if self.tag_name(grad) != "linearGradient":
            return

        if not self.needs_linear_defaults(grad):
            return

        grad.set("gradientUnits", "userSpaceOnUse")

        bx, by, bw, bh = bbox # pyright: ignore[reportUnusedVariable]

        # SVG defaults (objectBoundingBox → pixel)
        if grad.get("x1") is None:
            grad.set("x1", str(bx))
        if grad.get("y1") is None:
            grad.set("y1", str(by))
        if grad.get("x2") is None:
            grad.set("x2", str(bx + bw))
        if grad.get("y2") is None:
            grad.set("y2", str(by))


    def needs_radial_defaults(self, grad:RadialGradient) -> bool:
        """
        Return True when a radialGradient has no explicit geometric definition
        (cx, cy, r), meaning it still relies entirely on SVG's objectBoundingBox
        defaults.

        This predicate is used before injecting fallback coordinates or before
        normalizing the gradient to userSpaceOnUse, allowing callers to detect
        radial gradients that define no center or radius of their own.
        """

        return (
            grad.get("cx") is None and
            grad.get("cy") is None and
            grad.get("r")  is None
        )


    def apply_radial_defaults(self, grad:RadialGradient, bbox:tuple[float,float,float,float]) -> None:
        """
        Apply SVG default geometry for a radialGradient when no explicit center
        or radius is defined, converting the gradient into a fully user-space
        form.

        The function first verifies that the element is a radialGradient and that
        (cx, cy, r) are all missing, meaning the gradient still relies on
        objectBoundingBox defaults. In that case, gradientUnits is switched to
        userSpaceOnUse and pixel-space defaults are injected based on the
        provided bounding box (bx, by, bw, bh):

            cx = bx + 0.5 * bw
            cy = by + 0.5 * bh
            r  = 0.5 * min(bw, bh)

        Focal points (fx, fy) are also assigned when absent, matching the center.
        These values reproduce SVG's objectBoundingBox default radial geometry
        but in absolute user-space coordinates, ensuring deterministic behavior
        after gradient normalization and removal of gradientTransform.
        """

        if self.tag_name(grad) != "radialGradient":
            return

        if not self.needs_radial_defaults(grad):
            return

        grad.set("gradientUnits", "userSpaceOnUse")

        bx, by, bw, bh = bbox

        cx_default = bx + 0.5 * bw
        cy_default = by + 0.5 * bh
        r_default  = 0.5 * min(bw, bh)

        if grad.get("cx") is None:
            grad.set("cx", str(cx_default))
        if grad.get("cy") is None:
            grad.set("cy", str(cy_default))
        if grad.get("r") is None:
            grad.set("r", str(r_default))
        if grad.get("fx") is None:
            grad.set("fx", str(cx_default))
        if grad.get("fy") is None:
            grad.set("fy", str(cy_default))


    def apply_gradient_default_coords(self, grad:LinearGradient|RadialGradient, bbox:tuple[float,float,float,float]) -> None:
        """
        Dispatch default-coordinate injection to the appropriate gradient type.

        If the element is a linearGradient, apply_linear_defaults() is invoked to
        assign user-space fallback coordinates when all four linear endpoints are
        missing. If it is a radialGradient, apply_radial_defaults() is invoked to
        assign user-space center, radius, and focal-point defaults when no radial
        geometry is defined.

        This helper centralizes the tag-based routing so callers can normalize
        gradients without manually branching on gradient type.
        """

        tag = self.tag_name(grad)

        if tag == "linearGradient":
            self.apply_linear_defaults(grad, bbox)
        elif tag == "radialGradient":
            self.apply_radial_defaults(grad, bbox)


    def resolve_gradient_chain(self, grad:LinearGradient|RadialGradient) -> Transform:
        """
        Resolve an SVG gradient inheritance chain and determine the effective
        gradient transform according to SVG rules. This function flattens
        href-based gradient inheritance, merges attributes and stops from parent
        gradients, and resolves which gradientTransform should apply.

        The algorithm follows the SVG specification:

        - If the child gradient defines its own gradientTransform, all parent
        transforms are ignored.
        - If the child does not define a gradientTransform, the first parent in
        the href chain that defines one is used.

        During resolution, the function:

        - follows both href and xlink:href references
        - performs cycle detection to avoid infinite loops
        - inherits all non-transform attributes from parent gradients
        - inherits parent stops when the child has none
        - removes href attributes from the working clone once processed

        The final gradientTransform is returned as a Transform object. If no
        transform is found anywhere in the chain, an identity transform is used.

        Args:
            grad (LinearGradient | RadialGradient):
                The gradient element whose inheritance chain should be resolved.

        Returns:
            Transform:
                The resolved gradient transform. If no transform is present in
                the chain, an identity transform is returned.
        """

        g = grad
        seen = set()

        # SVG namespace for stop lookup
        svg_ns = self.svg.nsmap.get(None, "http://www.w3.org/2000/svg")

        while True:
            gid = g.get("id", "")
            self.log(logging.DEBUG, f"Resolving gradient {self.node_str(g)}")

            # Cycle detection
            if gid in seen:
                self.log(logging.DEBUG, "    STOP: cycle detected")
                break
            if gid:
                seen.add(gid)

            # Follow href chain (both plain and xlink)
            ref, _ = self.ref_target(g)

            if ref is None:
                break
            
            self.log(logging.DEBUG, f"Parent gradient → {self.node_str(ref)}")

            # Inherit attributes
            for attr, val in ref.attrib.items():
                if attr in ("id", "gradientTransform"):
                    continue
                if attr not in grad.attrib:
                    grad.set(attr, val)

            # Inherit stops if none present on the working clone
            has_stops = any(
                True
                for _ in grad.iterfind(f".//{{{svg_ns}}}stop")
            )
            if not has_stops:
                parent_stops = list(ref.iterfind(f".//{{{svg_ns}}}stop"))
                for stop in parent_stops:
                    self.add_node(copy.deepcopy(stop), grad)

                self.log(logging.DEBUG, f"Inheriting {len(parent_stops)} stops from parent {ref.get('id')}")

            # Drop href on the working clone
            grad.attrib.pop("href", None)
            grad.attrib.pop(f"{{{self.XLINK_NS}}}href", None)

            g = ref

        #--- get gradientTrasform ---
        gt = grad.attrib.get("gradientTransform", None)
        if gt is None:
            self.log(logging.DEBUG, "  No gradientTransform found → identity")
            T_gradient = Transform()
        else:
            self.log(logging.DEBUG, f"  gradientTransform found → {str(gt)}")
            T_gradient = Transform(gt)

        gid = grad.get("id", "")
        self.log(logging.DEBUG, f"Resolved {self.node_str(grad)}")

        return T_gradient


    def iter_geometry_subtree(self, grp:BaseElement, attr:str, pop_attr:bool=False, only_gt7_geometry:bool=False, 
                              CTM:Transform=Transform(), apply_transform:bool=False) -> Iterator[tuple[BaseElement, Transform]]:
        """
        Iterate through a group subtree and yield geometry nodes together with
        their accumulated transform, optionally filtering by inherited attribute
        and optionally applying the full CTM to each yielded node.

        The iterator performs a depth-first traversal using an explicit stack:

        - If attr is None or an empty string, the subtree is traversed without
        attribute filtering and all geometry nodes are yielded.

        - Otherwise, only nodes (and subgroups) that do not override the given
        attribute are descended into or yielded. Any node with a local value
        for attr is skipped.

        - Non-geometry nodes are ignored according to is_geometry(), with optional
        GT7-only filtering.

        - The combined transform CTM is updated at each step using the parent
        transform multiplied by the node's local transform. When apply_transform
        is True, this CTM is baked into the node before yielding.

        - If pop_attr is True and a real attribute name was provided, the inherited
        attribute is removed from yielded nodes and from the root group after
        traversal.

        The function yields (node, CTM) pairs for all geometry nodes that satisfy
        the inheritance rules.
        """

        # sentinel means "no attribute check"
        no_attr_check = (attr is None) or (attr == "")

        self.log(logging.DEBUG, f"[ITER] Iterating {self.node_str(grp)}, CTM={CTM}")

        stack = [(grp, CTM)]

        while stack:
            (g, parent_transform) = stack.pop()

            for node in g:
                local_transform = self.local_transform(node)
                CTM = parent_transform @ local_transform

                self.log(logging.DEBUG, f"[ITER] Found {self.node_str(node)}, CTM={CTM}")

                # Recurse into subgroups
                if self.tag_name(node) == "g":
                    # always descend when sentinel is used
                    if no_attr_check or node.get(attr) is None:
                        stack.append((node, CTM))
                    else:
                        self.log(logging.DEBUG, f"[ITER] Ignoring {self.node_str(node)} (overrides '{attr}')")
                    continue

                # Skip non-geometry nodes
                if not self.is_geometry(node, only_gt7_supported=only_gt7_geometry):
                    self.log(logging.DEBUG, f"[ITER] Ignoring {self.node_str(node)} (no geometry)")
                    continue

                # Yield logic: if sentinel, yield all geometry; otherwise yield only those that inherit attr
                if no_attr_check or node.get(attr) is None:
                    if apply_transform:
                        _, node = self.apply_transform_to_node(node, CTM)
                        CTM = Transform()

                    if pop_attr and not no_attr_check:
                        node.attrib.pop(attr, None)

                    self.log(logging.DEBUG, f"[ITER] Yielding {self.node_str(node)} for attribute '{attr}', CTM={CTM}")
                    yield node, CTM

        # Only pop the attribute when a real attribute name was provided
        if pop_attr and not no_attr_check:
            grp.attrib.pop(attr, None)


    def resolve_gradient_for_group(self, group:Group) -> int:
        """
        Flatten and normalize all gradients referenced by shapes inside a group,
        producing GT7-safe, fully resolved gradient definitions and rewiring each
        shape to its own baked copy.

        For each of the attributes “fill” and “stroke”, the routine:

        - Locates the gradient referenced by the group (linear, radial, or mesh).
        - For mesh gradients, delegates to replace_meshgradient_on_group().
        - Clones the original gradient and resolves any xlink:href /
        gradientTransform chain, yielding a single transform T_gradient.
        - Iterates all geometry nodes in the group that inherit the attribute
        using iter_geometry_subtree(), removing the inherited attribute from
        the subtree as it goes.
        - For each shape:
        - Clones the resolved gradient.
        - Applies default coordinates (linear or radial) based on the shape's
            bounding box.
        - Converts objectBoundingBox units to userSpaceOnUse and folds the
            shape's transform chain into the gradient's coordinates.
        - Applies T_gradient and removes any remaining gradientTransform.
        - Removes xlink:href to ensure the gradient is self-contained.
        - Normalizes stops to a two-stop, alpha-stripped form.
        - Registers the new gradient in <defs>.
        - Rewrites the shape's attribute to reference the new gradient.

        Returns the number of shapes whose gradients were replaced.
        """

        changed = 0

        for attr in ("fill", "stroke"):
            self.log(logging.DEBUG, f"Group = {self.node_str(group)}")

            grad, _ = self.ref_target(group, attr, tag_name={"meshgradient", "meshGradient", "linearGradient", "radialGradient"})
            if grad is None:
                continue

            tag = self.tag_name(grad).lower()

            if tag == "meshgradient":
                self.replace_meshgradient_on_group(group, attr, grad)
                continue

            # clone original gradient definition
            resolved_grad = self.clone_gradient(grad)
            
            # resolve chain on the cloned gradient, get chain transform
            T_gradient = self.resolve_gradient_chain(resolved_grad)
            self.log(logging.DEBUG, f"Resolved {self.node_str(resolved_grad)}")

            for shape, _ in self.iter_geometry_subtree(group, attr, pop_attr=True, only_gt7_geometry=False):
                
                self.log(logging.DEBUG, f"Original {self.node_str(grad)}")

                new_grad = self.clone_gradient(resolved_grad)

                bbox = self.shape_bbox(shape)
                if bbox is not None:
                    self.apply_gradient_default_coords(new_grad, bbox)

                # normailze coordinates to userSpace
                self.normalize_gradient_units(new_grad, shape)
                self.log(logging.DEBUG, f"normalized to userSpace: {self.node_str(new_grad)}")

                # apply gradient transform (shape transform is handled later in apply_all_transforms)
                self.apply_transform_to_gradient(new_grad, T_gradient)
                new_grad.attrib.pop("gradientTransform", None)
                self.log(logging.DEBUG, f"gradientTransform applied: {self.node_str(new_grad)}")

                # safety: no xlink:href left
                new_grad.attrib.pop("xlink:href", None)

                # normalize stops
                self.normalize_gradient_stops_and_colors(new_grad)
                self.log(logging.DEBUG, f"normalized stops and colors: {self.node_str(new_grad)}")

                # ensure defs and register new gradient
                defs = self.ensure_defs()
                self.add_node(new_grad, defs)

                # rewire shape to the new gradient id
                shape.set(attr, self.node_or_id_to_url(new_grad))
                self.log(logging.DEBUG, f"  New gradient assigned to {self.node_str(shape)}")

                changed += 1

        return changed


    def resolve_gradient_for_shape(self, shape:BaseElement) -> int:
        """
        Resolve and flatten any gradient referenced directly by a shape, producing
        a GT7-safe, fully normalized gradient definition and rewiring the shape to
        its own baked copy.

        For each of the attributes “fill” and “stroke”, the routine:

        - Locates the gradient referenced by the shape (linear, radial, or mesh).
        - For mesh gradients, delegates to replace_meshgradient().
        - Clones the original gradient and resolves any xlink:href /
        gradientTransform chain, yielding a single transform T_gradient.
        - Applies default coordinates (linear or radial) based on the shape's
        bounding box.
        - Converts objectBoundingBox units to userSpaceOnUse and folds the shape's
        transform chain into the gradient's coordinates.
        - Applies T_gradient and removes any remaining gradientTransform.
        - Removes xlink:href to ensure the gradient is self-contained.
        - Normalizes stops to a two-stop, alpha-stripped form.
        - Registers the new gradient in <defs>.
        - Rewrites the shape's attribute to reference the new gradient.

        Returns the number of attributes (fill/stroke) whose gradients were
        replaced.
        """

        changed = 0

        for attr in ("fill", "stroke"):
            self.log(logging.DEBUG, f"Shape = {self.node_str(shape)}")

            grad, _ = self.ref_target(shape, attr, tag_name={
                "meshgradient", "meshGradient","linearGradient", "radialGradient"
            })
            if grad is None:
                continue

            tag = self.tag_name(grad).lower()

            if tag == "meshgradient":
                self.replace_meshgradient(shape, attr, grad)
                continue

            self.log(logging.DEBUG, f"Original {self.node_str(grad)}")

            # clone original gradient definition
            new_grad = self.clone_gradient(grad)

            # resolve chain on the cloned gradient, get chain transform
            T_gradient = self.resolve_gradient_chain(new_grad)
            self.log(logging.DEBUG, f"Resolved {self.node_str(new_grad)}")

            bbox = self.shape_bbox(shape)
            if bbox is not None:
                self.apply_gradient_default_coords(new_grad, bbox)

            # normailze coordinates to userSpace
            self.normalize_gradient_units(new_grad, shape)

            self.log(logging.DEBUG, f"normalized to userSpace: {self.node_str(new_grad)}")

            # apply gradient transform (shape transform is handled later in apply_all_transforms)
            self.apply_transform_to_gradient(new_grad, T_gradient)
            new_grad.attrib.pop("gradientTransform", None)

            self.log(logging.DEBUG, f"gradientTransform resolved: {self.node_str(new_grad)}")

            # safety: no xlink:href left
            new_grad.attrib.pop("xlink:href", None)

            # normalize stops
            self.normalize_gradient_stops_and_colors(new_grad)

            self.log(logging.DEBUG, f"normalized stops and colors: {self.node_str(new_grad)}")

            # ensure defs and register new gradient
            defs = self.ensure_defs()
            self.add_node(new_grad, defs)

            # rewire shape to the new gradient id
            shape.set(attr, self.node_or_id_to_url(new_grad))
            self.log(logging.DEBUG, f"  New gradient assigned to {self.node_str(shape)}")

            changed += 1

        return changed

    def _apply_point(self, grad:LinearGradient|RadialGradient, T:Transform, x_attr:str, y_attr:str) -> None:
        """
        Apply a 2D transform to a single gradient coordinate pair (x_attr, y_attr)
        if both attributes are present on the gradient element.

        The routine reads the current numeric values for x_attr and y_attr,
        defaults them to 0 when missing, applies the transform T to the point,
        and writes the transformed coordinates back to the gradient. This helper
        is used by higher-level gradient normalization functions to keep the
        coordinate-update logic small, explicit, and reusable.
        """

        if x_attr in grad.attrib and y_attr in grad.attrib:
            x = float(grad.get(x_attr) or 0)
            y = float(grad.get(y_attr) or 0)
            x2, y2 = T.apply_to_point((x, y))
            grad.set(x_attr, str(x2))
            grad.set(y_attr, str(y2))


    def apply_transform_to_gradient(self, grad:LinearGradient|RadialGradient, T:Transform) -> None:
        """
        Apply a full CTM to a linear or radial gradient's coordinate attributes in
        a GT7-safe manner, correctly handling uniform and non-uniform scaling for
        radial radii.

        The routine dispatches by gradient type:

        1. Linear gradients.
        The attributes x1, y1, x2, and y2 are transformed directly using
        _apply_point(), producing fully baked user-space endpoints.

        2. Radial gradients.
        The center (cx, cy) and focal point (fx, fy) are transformed normally.
        The radius is scaled according to the CTM's effective scale:

            - If the scale is uniform (sx ≈ sy), the radius is multiplied by sx.
            - If the scale is non-uniform, GT7 cannot represent elliptical radial
                gradients. A warning is logged and the radius is approximated using
                the geometric mean sqrt(sx * sy).

        This function is used after gradientUnits normalization and after resolving
        any gradientTransform chain, ensuring that all gradient coordinates are
        expressed directly in user space with no remaining transform attributes.
        """

        tag = self.tag_name(grad)

        # Extract scale components from CTM
        a, b, tx = T.matrix[0] # pyright: ignore[reportUnusedVariable]
        c, d, ty = T.matrix[1] # pyright: ignore[reportUnusedVariable]

        # Uniform scale factor
        sx = (a*a + b*b)**0.5
        sy = (c*c + d*d)**0.5

        # --- LINEAR GRADIENT ---
        if tag == "linearGradient":
            self._apply_point(grad, T, "x1", "y1")
            self._apply_point(grad, T, "x2", "y2")
            return

        # --- RADIAL GRADIENT ---
        if tag == "radialGradient":

            # Transform center and focal point normally
            self._apply_point(grad, T, "cx", "cy")
            self._apply_point(grad, T, "fx", "fy")

            # Transform radius correctly
            r_attr = grad.get("r")

            if r_attr is not None:
                r = float(r_attr or 0)

                if abs(sx - sy) < 1e-9:
                    # Uniform scale → safe
                    grad.set("r", str(r * sx))
                else:
                    # Non-uniform scale → elliptical gradient
                    # GT7 cannot handle this → warn or approximate
                    self.log(logging.WARNING,
                        f"Non-uniform scale on radialGradient {grad.get('id')}: "
                        f"sx={sx}, sy={sy}. Converting to ellipse."
                    )
                    # Approximate: use geometric mean
                    grad.set("r", str(r * (sx * sy)**0.5))


    # endregion

    # region --- Pattern ---

    def normalize_units_for_pattern(self, geoms:List[PathElement], pat:Pattern, shape:BaseElement) -> List[PathElement]:
        """
        Convert a pattern's content units from objectBoundingBox to userSpaceOnUse
        by baking the shape's bounding-box transform into each geometry node and
        removing the patternContentUnits attribute.

        The routine performs the following steps:

        - Check patternContentUnits; if already userSpaceOnUse, return the geometry
        list unchanged.

        - Compute the bounding-box transform T_bbox for the shape using
        bbox_transform(), which represents translate(bx, by) followed by
        scale(bw, bh) in full user space.

        - Apply T_bbox to every geometry node in the pattern, producing a list of
        normalized, user-space geometry elements.

        - Remove patternContentUnits from the pattern element to finalize the
        conversion.

        Returns a new list of geometry nodes whose coordinates are fully expressed
        in user space and ready for GT7-safe tiling.
        """

        units = pat.get("patternContentUnits", "userSpaceOnUse")
        if units == "userSpaceOnUse":
            return geoms

        pat_id = pat.get("id", "")
        self.log(logging.DEBUG,
                f"[PAT] Converting pattern id={pat_id} from objectBoundingBox → userSpaceOnUse")

        # 1. Compute bounding box transform in user space
        T_bbox = self.bbox_transform(shape)
        self.log(logging.DEBUG, f"[PAT]   T_bbox={T_bbox}")

        # 2. Apply bbox transform to each geometry node
        normalized = []
        for g in geoms:
            _, g_flattened = self.apply_transform_to_node(g, T_bbox)
            normalized.append(g_flattened)

        # 3. Remove the attribute
        pat.attrib.pop("patternContentUnits", None)

        self.log(logging.DEBUG, f"[PAT] pattern id={pat_id} converted to userSpaceOnUse")

        return normalized


    def resolve_pattern_for_group(self, group:Group) -> int:
        """
        Resolve a <pattern> referenced by a group into GT7-safe, flattened,
        user-space geometry. The pattern is cloned for each shape in the group and
        expanded into explicit tiled geometry.

        The routine proceeds as follows:

        1. Locate the pattern referenced by the group's fill. If none exists,
        return 0. Remove the inherited fill from the group so shapes can be
        processed individually.

        2. Resolve the pattern's geometry using resolve_pattern(), flattening
        transforms, groups, and href chains. If the pattern contains no
        geometry, stop early.

        3. For each geometry-bearing shape in the group:
        Create a fresh <pattern> clone.
        Copy tiling attributes (x, y, width, height, patternUnits,
        patternTransform).
        Clone each geometry node, copying presentation attributes and stripping
        IDs.
        Insert the cloned pattern into <defs>.
        Rewire the shape's fill to the new pattern.
        Normalize pattern units using normalize_units_for_pattern().

        4. After all shapes are rewired, expand the pattern into explicit geometry
        via pattern_to_geometry(), tiling the pattern's geometry over each
        shape's bounding box and clipping the result.

        Returns:
            int: Number of shapes whose pattern fill was replaced.
        """

        # 1. Get structural <pattern> element
        pattern, _ = self.ref_target(group, "fill", tag_name="pattern")

        if pattern is None:
            return 0

        group.attrib.pop("fill", None)

        self.log(logging.DEBUG, f"[PAT] resolving {self.node_str(pattern)}")

        # 2. Resolve pattern geometry (flatten transforms, groups, href)
        geoms = self.resolve_pattern(pattern, Transform())
        if not geoms:
            self.log(logging.DEBUG, f"[PAT] no geometry in {self.node_str(pattern)}")
            # No geometry → remove pattern
            return 0

        # Gather shapes and attach pattern clone

        shapes = []
        for shape, _ in self.iter_geometry_subtree(group, "fill", pop_attr=True, only_gt7_geometry=False):
            shapes.append(shape)

            svg_ns = self.svg.nsmap.get(None, "http://www.w3.org/2000/svg")
            pattern_clone = inkex.etree.Element(f"{{{svg_ns}}}pattern")
    
            # copy tiling attributes from original pattern
            for attr in ("x", "y", "width", "height", "patternUnits", "patternTransform"):
                if attr in pattern.attrib:
                    pattern_clone.set(attr, pattern.get(attr))

            geoms_clone = []
    
            for g in geoms:
                g_copy = g.copy()
                self.copy_presentation_attributes(g, g_copy)
                g_copy.attrib.pop("id", None)
                
                pattern_clone.append(g_copy)
                geoms_clone.append(g_copy)
            
            pattern_clone.attrib.pop("id", None)
    
            defs = self.ensure_defs()
            pattern_clone = self.add_node(pattern_clone, defs)

            shape.set("fill", self.node_or_id_to_url(pattern_clone))

            # 3. Normalize pattern units (patternUnits + patternContentUnits)
            self.normalize_units_for_pattern(geoms_clone, pattern_clone, shape)

        # Generate geometry
        self.pattern_to_geometry(shapes)

        return len(shapes)


    def resolve_pattern_for_shape(self, shape:BaseElement):
        """
        Resolve a <pattern> referenced directly by a single shape, flatten its
        geometry into user-space coordinates, clone a GT7-safe pattern definition,
        and expand the pattern into explicit tiled geometry.

        The routine performs the following steps:

        1. Locate the pattern referenced by the shape's fill. If none exists,
        return 0.

        2. Resolve the pattern's geometry using resolve_pattern(), flattening
        transforms, groups, and href chains. If the pattern contains no
        geometry, remove the fill and stop early.

        3. Normalize pattern units using normalize_units_for_pattern(), converting
        objectBoundingBox content units into user-space geometry.

        4. Create a fresh <pattern> element and copy tiling attributes such as
        x, y, width, height, patternUnits, patternTransform, preserveAspectRatio,
        and viewBox.

        5. Append the normalized geometry nodes, strip IDs, and register the new
        pattern in <defs>.

        6. Rewire the shape's fill to reference the new pattern.

        7. Expand the pattern into explicit geometry via pattern_to_geometry(),
        tiling the pattern's geometry over the shape's bounding box and clipping
        the result.

        Returns:
            int: 1 when the pattern is successfully resolved and replaced.
        """

        # 1. Get structural <pattern> element
        pattern, _ = self.ref_target(shape, "fill", tag_name="pattern")
        if pattern is None:
            return 0

        self.log(logging.DEBUG, f"[PAT] resolving {self.node_str(pattern)}")

        # 2. Resolve pattern geometry (flatten transforms, groups, href)
        geoms = self.resolve_pattern(pattern, Transform())
        if not geoms:
            self.log(logging.DEBUG, f"[PAT] no geometry in {self.node_str(pattern)}")
            # No geometry → remove pattern
            shape.attrib.pop("fill", None)
            return 0

        # 3. Normalize pattern units (patternUnits + patternContentUnits)
        geoms = self.normalize_units_for_pattern(geoms, pattern, shape)
        
        self.log(logging.DEBUG, f"[PAT] normalized pattern geometry count={len(geoms)}")

        svg_ns = self.svg.nsmap.get(None, "http://www.w3.org/2000/svg")
        pattern_new = inkex.etree.Element(f"{{{svg_ns}}}pattern")

        # copy tiling attributes from original pattern
        for attr in ("x", "y", "width", "height", "patternUnits", "patternTransform", "preserveAspectRatio", "viewBox"):
            if attr in pattern.attrib:
                pattern_new.set(attr, pattern.get(attr))

        for g in geoms:
            pattern_new.append(g)
        
        pattern_new.attrib.pop("id", None)

        defs = self.ensure_defs()
        pattern_clone = self.add_node(pattern_new, defs)

        self.log_svg(pattern_clone, header="CLIPPED PATTERN")

        shape.set("fill", self.node_or_id_to_url(pattern_clone))

        self.pattern_to_geometry(shape)

        return 1


    def add_geom_preserving_zorder(self, parts:List[PathElement], geom:PathElement) -> None:
        """
        Append geometry to a list while preserving z-order, merging only with the
        current tail element when both share identical presentation attributes.

        The routine flattens the input (a single PathElement or a list) and
        processes each item in order:

        - Compute the presentation signature of the new item.
        - If the list already contains a tail element, compare signatures.
        When they match, merge the tail and the new item using combine_paths(),
        then restore the tail's presentation attributes onto the merged result.
        The merged geometry replaces the tail in-place, preserving z-order.
        - If signatures differ, append the new item normally.

        This function performs strictly local merging: only adjacent geometry
        with identical styling is combined, ensuring that drawing order remains
        unchanged and that no non-adjacent geometry is ever regrouped.
        """

        # flatten input
        new_items = geom if isinstance(geom, list) else [geom]

        for item in new_items:
            sig_new = self.presentation_signature(item)

            if parts:
                tail = parts[-1]
                sig_tail = self.presentation_signature(tail)

                # merge only if presentation signatures match
                if sig_new == sig_tail:
                    self.log(logging.DEBUG,f"Merging {self.node_str(item)}")
                    merged = self.combine_paths([tail, item])

                    # restore presentation attributes from tail
                    self.copy_presentation_attributes(tail, merged, override=True)
                    
                    parts[-1] = merged
                    continue

            # otherwise append normally
            self.log(logging.DEBUG,f"Adding {self.node_str(item)}")
            parts.append(item)


    def resolve_pattern(self, pattern:Pattern, transform=Transform()) -> List[PathElement]:
        """
        Flatten a <pattern> element into user-space geometry by resolving its own
        children, accumulating transforms, following href chains, and merging the
        referenced pattern's geometry when appropriate.

        The routine performs the following steps:

        - Resolve the pattern's direct geometry using resolve_pattern_geometry(),
        which flattens groups, applies transforms, converts shapes to paths, and
        returns a list of geometry nodes.

        - Check whether the pattern itself references another pattern via
        xlink:href. When a referenced pattern exists:
        - Recursively resolve the referenced pattern's geometry.
        - If the current pattern has no children, adopt the referenced geometry
            wholesale.
        - Copy tiling attributes (x, y, width, height, patternUnits,
            patternContentUnits, patternTransform) from the referenced pattern
            when they are not already present on the current pattern.

        - Return the final list of geometry nodes, expressed in user space and
        ready for unit normalization or tiling.

        This function does not perform unioning or clipping; it strictly flattens
        the pattern's structural and transform hierarchy.
        """

        self.log(logging.DEBUG, f"[PAT] resolve {self.node_str(pattern)} transform={transform}")

        parts = self.resolve_pattern_geometry(pattern)

        # handle href / xlink:href on <pattern> itself
        ref, _ = self.ref_target(pattern)
        if ref is not None:
            self.log(logging.DEBUG, f"[PAT]   pattern references {self.node_str(ref)}")

            ref_geom = self.resolve_pattern(ref, transform=transform)

            if len(pattern) == 0:
                parts = ref_geom

            # Copy tiling attributes from referenced pattern
            for attr in ("x", "y", "width", "height", "patternUnits", "patternContentUnits", "patternTransform"):
                if attr in ref.attrib and not attr in pattern.attrib:
                    pattern.set(attr, ref.get(attr))

        return parts

    def resolve_pattern_geometry(self, pattern:Pattern) -> List[PathElement]:
        """
        Flatten a <pattern> element into user-space geometry by resolving its
        children, converting all shapes to paths, preserving z-order, and clipping
        the result to the pattern's viewBox.

        The routine performs the following steps:

        - Log the original pattern for debugging.
        - Resolve geometry and references, then flatten the DOM structure so all
        transforms and groups are eliminated.
        - Iterate all geometry nodes using iter_geometry_subtree(), convert
        each to a path, copy presentation attributes, and append it to the
        result list while preserving z-order via add_geom_preserving_zorder().
        - Compute the pattern's viewBox and clip all paths against it using
        path_intersection().
        - Remove IDs from the resulting geometry nodes to ensure they are
        self-contained and safe for cloning.

        Returns a list of PathElement objects representing the fully flattened,
        clipped, user-space geometry of the pattern.
        """

        self.log_svg(pattern, header="ORIGINAL PATTERN")

        self.resolve_geometry(pattern)
        self.resolve_references(pattern)
        self.flatten_svg_dom(pattern)

        self.log_svg(pattern, header="FLATTENED PATTERN")

        parts = []

        for geom, _ in self.iter_geometry_subtree(pattern, attr="", only_gt7_geometry=False):
            path = self.convert_to_path(geom, Transform())
            if path is not None:
                self.copy_presentation_attributes(geom, path, override=True)
                parts.append(path)

            self.log(logging.DEBUG,f"Adding {str(path)}")
            self.add_geom_preserving_zorder(parts, path)


        viewbox = self.pattern_viewbox(pattern)
        parts = self.path_intersection(viewbox, parts)

        for part in parts:
            part.attrib.pop("id", None)

        return parts
    

    def pattern_viewbox(self, pattern:Pattern) -> PathElement:
        """
        Return a rectangular PathElement representing the pattern's tile box in
        pattern coordinate space.

        The rectangle is derived as follows:

        - If the pattern defines a viewBox, its (minX, minY, width, height) are
        used directly to construct the tile rectangle.

        - Otherwise, when patternUnits is userSpaceOnUse, the tile box is built
        from the pattern's x, y, width, and height attributes, which define the
        pattern's user-space tile region.

        The returned PathElement contains a simple four-point “M/L/Z” path and
        carries no presentation attributes. It is used for clipping pattern
        geometry after flattening.
        """

        # Case 1: pattern has a viewBox → use it directly
        vb = pattern.get("viewBox")
        if vb:
            minx, miny, w, h = map(float, vb.split())
            rect = inkex.PathElement()
            rect.set("d", f"M {minx} {miny} L {minx+w} {miny} L {minx+w} {miny+h} L {minx} {miny+h} Z")
            return rect

        # Case 2: patternUnits="userSpaceOnUse" (most common)
        # width/height define the tile box directly
        w = float(pattern.get("width") or 0)
        h = float(pattern.get("height") or 0)
        x = float(pattern.get("x") or 0)
        y = float(pattern.get("y") or 0)

        rect = inkex.PathElement()
        rect.set("d", f"M {x} {y} L {x+w} {y} L {x+w} {y+h} L {x} {y+h} Z")
        return rect
    
    
    def presentation_signature(self, el:BaseElement) -> tuple[tuple[str, str | None], ...]:
        """
        Return a tuple of (attribute, value) pairs for all presentation attributes
        defined on the element, sorted by attribute name.

        The signature is used to detect identical styling when merging adjacent
        geometry. Each entry is a (str, str|None) pair, and the outer tuple
        contains one entry per presentation attribute. This makes the signature
        stable, hashable, and suitable for grouping or comparison operations.
        """

        return tuple((attr, el.get(attr)) for attr in sorted(self.PRESENTATION_ATTRS))


    def iter_pattern_tiles(self, el:BaseElement, pattern:Pattern) -> Iterator[tuple[float, float]]:
        """
        Yield the (dx, dy) tile origins for a pattern so that all tiles covering a
        shape's bounding box are enumerated in pattern-space coordinates.

        The routine computes tile placement as follows:

        - Read the pattern's x, y, width, and height to determine the tile size
        and untransformed origin.

        - Apply the pattern's own transform (if present) to the origin so that
        tiling occurs in the correct coordinate space.

        - Compute the starting tile indices (row0, col0) by locating the tile that
        covers the shape's top-left corner, then subtracting a small margin to
        ensure outer tiles are included.

        - Compute the number of rows and columns required to cover the entire
        bounding box, again with a margin to avoid missing edge tiles.

        - Yield each tile origin (dx, dy) in pattern space, suitable for later
        geometry cloning and placement.

        This iterator does not perform clipping or geometry expansion; it only
        provides tile origins for higher-level tiling logic such as pattern_to_geometry().
        """

        bbox = el.bounding_box()

        px = float(pattern.get("x") or 0)
        py = float(pattern.get("y") or 0)
        pw = float(pattern.get("width") or 0)
        ph = float(pattern.get("height") or 0)

        if pw <= 0 or ph <= 0:
            self.log(logging.DEBUG,
                    f"Removing pattern {self.node_str(pattern)}, invalid width/height: width={pw}, height={ph}")
            return

        # pattern transform (if any)
        pattern_t = inkex.Transform(pattern.get("transform")) if pattern.get("transform") else inkex.Transform()

        # CORRECT: apply transform to the pattern origin
        origin = pattern_t.apply_to_point(inkex.Vector2d(px, py))

        # Compute tile indices covering bbox
        col0 = math.floor((bbox.left - origin.x) / pw) - 2
        row0 = math.floor((bbox.top  - origin.y) / ph) - 2

        start_x = origin.x + col0 * pw
        start_y = origin.y + row0 * ph

        # +2 margin ensures outer tiles are included (fixes missing row/column)
        cols = math.ceil((bbox.right  - start_x) / pw) + 4
        rows = math.ceil((bbox.bottom - start_y) / ph) + 4

        for r in range(rows):
            for c in range(cols):
                dx = origin.x + (col0 + c) * pw
                dy = origin.y + (row0 + r) * ph
                yield dx, dy

    def pattern_viewbox_transform(self, pattern:Pattern) -> Transform:
        """
        Compute the browser-accurate transform that maps a pattern's viewBox into
        its tile rectangle, producing the same coordinate normalization that SVG
        renderers apply before pattern tiling.

        The transform is constructed in three stages:

        - Translate the viewBox origin (minX, minY) to (0, 0), aligning the
        viewBox coordinate system with the pattern's local origin.

        - Apply scaling derived from the ratio between the pattern's width/height
        and the viewBox width/height. When preserveAspectRatio is “none”, the
        scale is non-uniform (sx, sy). Otherwise, a uniform scale is chosen
        according to the meet/slice rule.

        - Apply alignment offsets based on the preserveAspectRatio keyword
        (xMin/xMid/xMax and YMin/YMid/YMax), shifting the scaled viewBox inside
        the pattern tile rectangle.

        The final transform is T_align ∘ T_scale ∘ T_translate. If the pattern
        has no viewBox or invalid width/height, an identity transform is returned.
        """

        # No viewBox → identity
        vb = pattern.get("viewBox")
        if not vb:
            self.log(logging.DEBUG, "[PAT] {self.node_str(pattern)} has no viewbox")
            return inkex.Transform()

        # Parse viewBox
        minX, minY, vbWidth, vbHeight = map(float, vb.split())

        # Tile rectangle
        pw = float(pattern.get("width") or 0)
        ph = float(pattern.get("height") or 0)

        if pw <= 0 or ph <= 0:
            self.log(logging.DEBUG, f"[PAT] {self.node_str(pattern)} has invalid viewbox")
            return inkex.Transform()

        # 1. Translate viewBox origin to (0,0)
        T_translate = inkex.Transform().add_translate(-minX, -minY)
        self.log(logging.DEBUG, f"[PAT] {self.node_str(pattern)}, viewbox translate={T_translate}")

        # 2. Compute scaling
        sx = pw / vbWidth
        sy = ph / vbHeight

        # Parse preserveAspectRatio
        # align keyword	alignX	alignY
        # xMinYMin	    0       0
        # xMidYMin	    0.5	    0
        # xMaxYMin	    1	    0
        # xMinYMid	    0	    0.5
        # xMidYMid	    0.5	    0.5
        # xMaxYMid	    1	    0.5
        # xMinYMax	    0	    1
        # xMidYMax	    0.5	    1
        # xMaxYMax	    1	    1

        par = pattern.get("preserveAspectRatio", "xMidYMid")
        assert par is not None
        align, meet_or_slice = par.split() if " " in par else (par, "meet")

        # Determine uniform scale if needed
        if meet_or_slice == "none":
            s = None  # non-uniform scaling
        else:
            s = min(sx, sy) if meet_or_slice == "meet" else max(sx, sy)

        # Build scale transform
        if s is None:
            T_scale = inkex.Transform().add_scale(sx, sy)
            self.log(logging.DEBUG, f"[PAT] {self.node_str(pattern)}, viewbox scale={T_scale}")
        else:
            T_scale = inkex.Transform().add_scale(s, s)
            self.log(logging.DEBUG, f"[PAT] {self.node_str(pattern)}, viewbox scale={T_scale}")

        # 3. Alignment offsets
        alignX = {"xMin": 0, "xMid": 0.5, "xMax": 1}[align[:4]]
        alignY = {"YMin": 0, "YMid": 0.5, "YMax": 1}[align[4:]]

        # Compute alignment translation
        if s is None:
            dx = 0
            dy = 0
        else:
            dx = (pw - vbWidth * s) * alignX
            dy = (ph - vbHeight * s) * alignY

        T_align = inkex.Transform().add_translate(dx, dy)
        T_final = T_align @ T_scale @ T_translate

        self.log(logging.DEBUG, f"[PAT] {self.node_str(pattern)}, viewbox scale={T_final}")

        # Final transform
        return T_final


    def tiles_can_be_merged(self, node:PathElement) -> bool:
        """
        Return True when a pattern tile created from the given geometry node can
        be merged with adjacent tiles without breaking gradient semantics.

        A tile is merge-safe only when:

        - The node is valid geometry according to is_geometry().
        - Neither its fill nor stroke references any gradient (linear, radial, or
        mesh). Any url(#…) pointing to a gradient forces per-tile resolution and
        prevents merging.
        - The node does not carry a gradientTransform attribute, since this would
        require tile-local coordinate evaluation.

        If all conditions are satisfied, the tile can be merged into a single path
        during pattern expansion; otherwise, it must remain separate so that
        gradient behavior matches browser rendering.
        """

        # Must be geometry
        if not self.is_geometry(node, only_gt7_supported=False):
            return False

        # Fill must not reference a gradient
        fill = node.get("fill")
        if fill and fill.startswith("url("):
            target, _ = self.ref_target(node, "fill", {"meshgradient", "linearGradient", "radialGradient"})
            if target is not None:
                return False

        # Stroke must not reference a gradient
        stroke = node.get("stroke")
        if stroke and stroke.startswith("url("):
            target, _ = self.ref_target(node, "stroke", tag_name={"meshgradient", "linearGradient", "radialGradient"})
            if target is not None:
                return False

        # No gradientTransform on the node itself
        if node.get("gradientTransform"):
            return False

        return True


    def merge_tiles(self, node:PathElement, el:BaseElement, pattern:Pattern) -> PathElement:
        """
        Merge all pattern tiles generated from a geometry node into a single
        PathElement, applying viewBox normalization, tile placement transforms,
        and the patternTransform, producing a fully baked, GT7-safe merged tile.

        The routine performs the following steps:

        - Compute the viewBox transform for the pattern using pattern_viewbox_transform(),
        which maps pattern viewBox coordinates into the pattern's tile
        rectangle.

        - Iterate all tile origins produced by iter_pattern_tiles(), cloning the
        input geometry for each tile.

        - Apply the tile's translation and the viewBox transform to the clone,
        yielding a user-space tile positioned exactly as the browser would
        render it.

        - Incrementally merge tiles using combine_paths(), preserving z-order and
        producing a single geometry node. Presentation attributes from the
        original node are restored after merging.

        - Apply the patternTransform to the merged geometry, folding all pattern
        transforms into the final path.

        Returns a single PathElement representing the fully merged pattern tile
        geometry. This function is used only when tiles_can_be_merged() indicates that
        gradient semantics will remain correct.
        """

        viewbox_t = self.pattern_viewbox_transform(pattern)

        log_first_tile = True
        merged = None

        for dx, dy in self.iter_pattern_tiles(el, pattern):
            clone = self.convert_to_path(node)
            if clone is None:
                continue

            # tile transform only
            tile_t = inkex.Transform().add_translate(dx, dy)
            self.transform_path(clone, tile_t @ viewbox_t)
            
            # incremental merge
            if log_first_tile:
                self.log(logging.DEBUG, f"Merging tile {self.node_str(clone)}")
                log_first_tile = False

            if merged is None:
                merged = clone
            else:
                merged = self.combine_paths([merged, clone])

        self.copy_presentation_attributes(node, merged)

        raw_pt = pattern.get("patternTransform")
        pattern_t = inkex.Transform(raw_pt) if raw_pt else inkex.Transform()
        _, merged = self.apply_transform_to_node(merged, pattern_t)

        return merged
    

    def append_tiles(self, node:PathElement, el:BaseElement, pattern:Pattern) -> None | list[PathElement]:
        """
        Expand a pattern into explicit per-tile geometry for a single node, fully
        baking transforms, gradient semantics, and viewBox normalization so that
        each tile matches browser rendering in user-space coordinates.

        The routine performs the following steps:

        - Resolve any gradient used by the node via resolve_gradient_for_shape(),
        ensuring that fill/stroke gradients are flattened, GT7-safe, and
        independent of the pattern's transform chain.

        - Parse patternTransform and compute the pattern's viewBox transform using
        pattern_viewbox_transform(). These transforms are folded into each tile's placement.

        - Iterate all tile origins produced by iter_pattern_tiles(). For each tile:
        - Clone the node and copy its presentation attributes.
        - Build the full tile transform: patternTransform ∘ tileTranslate ∘
            viewBoxTransform.
        - If the node's fill or stroke references a gradient, clone that
            gradient, apply the full tile transform to its coordinates, register
            the cloned gradient in <defs>, and rewire the tile to use it.
        - Apply the full transform to the tile's path geometry.
        - Append the tile to the output list.

        - Return the list of fully transformed tile geometries, or None if no
        tiles were generated.

        This function is used when tiles cannot be merged safely (see tiles_can_be_merged(), 
        ensuring that each tile receives its own correctly transformed gradient and
        geometry.
        """

        self.resolve_gradient_for_shape(node)

        raw_pt = pattern.get("patternTransform")
        pattern_t = inkex.Transform(raw_pt) if raw_pt else inkex.Transform()
        self.log(logging.DEBUG, f"Pattern transform = {pattern_t}")

        viewbox_t = self.pattern_viewbox_transform(pattern)        

        tiles = []
        log_first_tile = True

        for dx, dy in self.iter_pattern_tiles(el, pattern):
            clone = self.convert_to_path(node)
            if clone is None:
                continue

            self.copy_presentation_attributes(node, clone)

            # tile transform + pattern transform
            tile_t = inkex.Transform().add_translate(dx, dy)
            total_t = pattern_t @ tile_t @ viewbox_t

            for presentation_attribute in ["fill", "stroke"]:
                gradient, _ = self.ref_target(node, presentation_attribute, tag_name={"meshgradient", "linearGradient", "radialGradient"})

                if log_first_tile:
                    self.log(logging.DEBUG, f"{presentation_attribute} = {self.node_str(gradient)}")

                if not gradient is None:
                    if log_first_tile:
                        self.log(logging.DEBUG, f"Original gradient = {self.node_str(gradient)}")

                    gradient_clone = self.clone_gradient(gradient)
                    gradient_clone.attrib.pop("id", None)
                    self.apply_transform_to_gradient(gradient_clone, total_t)
            
                    defs = self.ensure_defs()
                    self.add_node(gradient_clone, defs)
                    clone.set(presentation_attribute, self.node_or_id_to_url(gradient_clone))

                    if log_first_tile:
                        self.log(logging.DEBUG, f"Transformed gradient = {self.node_str(gradient_clone)}")

                    log_first_tile = False

            # tile transform only
            self.transform_path(clone, total_t)
            
            tiles.append(clone)

            self.log(logging.DEBUG, f"appended = {self.node_str(clone)}, children = {len(tiles)}")


        if len(tiles) == 0:
            return None

        return tiles

    @overload
    def pattern_to_geometry(self, elements: BaseElement) -> int: ...
    @overload
    def pattern_to_geometry(self, elements: Iterable[BaseElement]) -> int: ...

    def pattern_to_geometry(self, elements) -> int:
        """
        Expand a pattern fill into explicit geometry for one or more elements,
        producing GT7-safe, fully baked user-space paths that replicate browser
        pattern rendering. This is the top-level pattern-expansion routine: it
        tiles, merges, clips, transforms, and finally inserts geometry into the
        DOM in correct paint-order.

        The pipeline proceeds as follows:

        - Normalize the input into a list of elements. Each element is processed
        independently.

        - For each element:
        - Resolve the referenced <pattern>. If none exists, skip.
        - Collect the pattern's child geometry nodes. If the pattern is empty,
            remove the fill and continue.
        - For each child node:
            - Determine whether tiles can be merged using tiles_can_be_merged().
            - If merge-safe, call merge_tiles() to produce a single
            merged path.
            - Otherwise, call append_tiles() to generate a list of
            individually transformed tiles.
            - Insert the resulting geometry (merged or per-tile) into the
            pattern_geometry list.

        - If pattern_geometry is non-empty:
            - Record the element's unclipped clone as a clipping path.
            - Record the geometry layers for later intersection.
            - Track the element for final DOM insertion.
        - Otherwise:
            - Remove the fill and continue.

        - Clip all geometry layers against their corresponding clipping paths
        using path_intersection().

        - Insert clipped geometry into the DOM:
        - Respect paint-order: if fill is above stroke, insert tiles one slot
            higher than the element's index.
        - Apply the element's local transform to each tile via
            apply_transform_to_node().
        - Remove any residual transform attributes and append the tile to the
            parent.

        - Remove the element's fill. If the element has no visible stroke (none or
        zero width), remove the element entirely.

        Returns 1 when at least one element was processed. This function is the
        final stage of the pattern pipeline, combining tiling, merging, gradient
        handling, viewBox normalization, clipping, and DOM insertion into a single
        browser-accurate expansion step.
        """

        if not isinstance(elements, (list, tuple)):
            elements = [elements] 

        count = 0

        clippaths = []
        geometry = []
        resolved_elements = []

        for el in elements:
            self.log(logging.DEBUG, f"Resolving pattern for {self.node_str(el)}")

            pattern_geometry = []

            # 0. Resolve referenced pattern
            pattern, pattern_id = self.ref_target(el, "fill", tag_name="pattern")
            if pattern is None:
                continue

            nodes = list(pattern.iterchildren())
            if not nodes:
                el.set("fill", "none")
                count += 1
                continue

            for node in nodes:
                
                can_be_merged = self.tiles_can_be_merged(node)
                self.log(logging.DEBUG, f"Can be merged={can_be_merged}, {self.node_str(node)}")
                    
                if can_be_merged:
                    merged_shape = self.merge_tiles(node, el, pattern)
                    if not merged_shape is None:
                        pattern_geometry.insert(0, merged_shape)
                        count += 1
                else:
                    tiles = self.append_tiles(node, el, pattern)
                    if not tiles is None:
                        pattern_geometry[0:0] = tiles
                        count += 1

            if pattern_geometry:

                self.log(logging.DEBUG, f"Resolved pattern {self.node_str(pattern)} into {len(pattern_geometry)} layers")
                geometry.append(pattern_geometry)
                clippath = copy.deepcopy(el)
                clippath.attrib.pop("transform", None)
                clippaths.append(clippath)
                resolved_elements.append(el)

            else:
                el.set("fill", "none")
                self.log(logging.DEBUG, f"Removing pattern {self.node_str(pattern)}, could not resolve any geometry")
                count += 1

        # clipping
        
        clipped_geometry = self.path_intersection(clippaths, geometry)

        # Adding to DOM 

        fill_pos, stroke_pos, _ = self.parse_paint_order(el)
        
        for el_idx, resolved_el in enumerate(resolved_elements):
            parent, child_idx = self.parent_of(resolved_el)

            # Adhere to paint-order, place tiles above stroke if fill is above stroke
            if fill_pos > stroke_pos:
                child_idx += 1

            self.log(logging.DEBUG, f"[PAT] Adding below parent {self.node_str(parent)} at index {child_idx}")

            shape_tr = self.local_transform(resolved_el)
            
            clipped_pattern_geometry = clipped_geometry[el_idx]
            if not clipped_pattern_geometry is None:

                for tile in clipped_pattern_geometry:

                    self.log(logging.DEBUG, f"local transform = {str(shape_tr)}")
                    _, tile = self.apply_transform_to_node(tile, shape_tr)
                    tile.attrib.pop("transform", None)

                    self.add_node(tile, parent, child_idx)
                    self.log(logging.DEBUG, f"Intersected tile {self.node_str(tile)}")

            # Remove fill and check if el becomes invisible (has no stroke) --> remove in that case
            # <-- tried to move el.set("fill", "none")  here
            resolved_el.set("fill", "none") #<-- moved this line

            stroke = self.inherit_attribute(resolved_el, "stroke")
            stroke_width = self.inherit_attribute(resolved_el, "stroke-width")

            # Normalize stroke-width
            has_zero_stroke = False
            if stroke_width is not None:
                try:
                    has_zero_stroke = float(str(stroke_width).rstrip("px")) == 0.0
                except ValueError:
                    has_zero_stroke = False

            # Invisible if: stroke is none OR stroke-width is zero
            if (stroke is None or stroke.strip().lower() == "none") or has_zero_stroke:
                self.remove_node(resolved_el)


        self.log(logging.DEBUG,
                f"Expanded pattern '{pattern_id}' into geometry on {self.node_str(el)}")

        return 1


    # endregion

    # region --- Clipping ---
    

    def resolve_clippath(self, cp:ClipPath, transform:Transform = Transform()) -> None|PathElement:
        """
        Resolve a <clipPath> into a single flattened PathElement in user-space by
        accumulating transforms, flattening child geometry, unioning all parts,
        and applying any referenced clipPath via xlink:href.

        The routine performs the following steps:

        - Accumulate the clipPath's own transform into the incoming CTM, producing
        the effective transform for all children.

        - For each child of the <clipPath>, call resolve_clippath_geometry()
        to flatten transforms, convert shapes to paths, and apply any
        clip-paths on the child itself. Non-empty geometry is collected.

        - Union all child geometry using path_union(), producing the primary
        clipping shape.

        - If the <clipPath> references another <clipPath> via xlink:href:
        - Recursively resolve the referenced clipPath.
        - Intersect the referenced geometry with the current geometry using
            path_intersection().
        - If the intersection is empty, return None; otherwise, strip any
            residual clip-path attribute.

        - Return the final PathElement representing the fully resolved clipping
        geometry. This geometry is in pure user-space coordinates and ready for
        application to shapes or pattern tiles.
        """

        self.log(logging.DEBUG, f"[CP] id={cp.get('id')}, {self.node_str(cp)}, M={transform}")
        self.log(logging.DEBUG, f"[CP] children={[self.node_str(c) for c in cp]}")

        # accumulate transform on <clipPath>
        if cp.get("transform"):
            t = Transform(cp.get("transform"))
        else:
            t = Transform()

        transform = transform @ t
        self.log(logging.DEBUG, f"[CP]   accumulated-M={transform}")

        # unify children WITHOUT applying their clip-paths
        parts = []
        for child in cp:
            self.log(logging.DEBUG, f"[CP]   → resolve child {self.node_str(child)}")
            geom = self.resolve_clippath_geometry(child, transform)

            if not geom is None and not self.is_empty_path(geom):
                self.log(logging.DEBUG, f"[CP]   → child geometry {self.node_str(geom)}")
                parts.append(geom)

        geom = self.path_union(parts)

        # handle href / xlink:href on <clipPath> itself
        ref, _ = self.ref_target(cp)
        if ref is not None:
            self.log(logging.DEBUG, f"[CP]   clipPath references {self.node_str(cp)}")

            ref_geom = self.resolve_clippath(ref, transform)
            clipped = self.path_intersection(ref_geom, [geom])

            if len(clipped) == 1:
                geom = clipped[0]
                geom.attrib.pop("clip-path", None)

            else:
                self.log(logging.DEBUG, f"[CP] {self.node_str(cp)} resolved to empty path")        
                return None

        self.log(logging.DEBUG, f"[CP] resolve_clippath DONE {self.node_str(geom)}")

        return geom


    def resolve_clippath_geometry(self, node:BaseElement, M:Transform) -> PathElement | None:
        """
        Resolve a single node inside a <clipPath> into flattened user-space
        geometry by accumulating transforms, flattening groups, converting shapes
        to paths, and applying any nested clip-paths. This is the core recursive
        routine used by resolve_clippath().

        The routine proceeds as follows:

        - Accumulate the node's own transform into the incoming CTM M, producing
        the effective transform for all geometry under this node.

        - If the node is geometry, convert it to a path using convert_to_path()
        with the accumulated transform. The result is a single PathElement.

        - If the node is a group (<g>), recursively resolve each child via
        resolve_clippath_geometry(), collect non-empty geometry, and union the
        results using path_union().

        - If the node is neither geometry nor a group, return an empty path.

        - If the node itself carries a clip-path attribute:
        - Resolve the referenced <clipPath> using
            resolve_clippath().
        - Intersect the node's geometry with the referenced clip geometry using
            path_intersection().
        - If the intersection is empty, return None; otherwise, adopt the
            clipped geometry.

        - Return the final PathElement representing the node's fully resolved
        clipping geometry in user-space. This geometry is guaranteed to have all
        transforms baked in and all nested clip-paths applied.
        """

        self.log(logging.DEBUG, f"[CP] node={self.node_str(node)} M={M}")
        self.log(logging.DEBUG, f"[CP] is_geometry={self.is_geometry(node, only_gt7_supported=True)} clip-path={node.get('clip-path')}")

        # accumulate transform
        if node.get("transform"):
            t = Transform(node.get("transform"))
            M = M @ t

            self.log(logging.DEBUG, f"[CP]     node-transform={t}")
            self.log(logging.DEBUG, f"[CP]     accumulated-M={M}")

        tag = self.tag_name(node)

        # geometry → path
        if self.is_geometry(node, only_gt7_supported=True):
            geom = self.convert_to_path(node, M)
            self.log(logging.DEBUG, f"[CP]     converted geometry={self.node_str(geom)}")

        # group → unify children
        elif tag == "g":
            parts = []
            for child in node:
                self.log(logging.DEBUG, f"[CP]       → child {self.node_str(child)}")

                p = self.resolve_clippath_geometry(child, M)

                if p is None:
                    self.log(logging.DEBUG, f"[CP] → resolved empty clippath")

                else:
                    self.log(logging.DEBUG, f"[CP] → resolved group geometry {self.node_str(p)}")

                    if not p is None and not self.is_empty_path(p):
                        parts.append(p)

            geom = self.path_union(parts)

        else:
            return self.empty_path()

        # apply clip-path to geometry (if any)
        cp, _ = self.ref_target(node, "clip-path")
        if cp is not None:
            self.log(logging.DEBUG, f"[CP] resolving nested {self.node_str(cp)}")

            clip_geom = self.resolve_clippath(cp, M)

            if clip_geom is None:
                self.log(logging.DEBUG, f"[CP] nested clip-path is empty")
            else:
                self.log(logging.DEBUG, f"[CP] intersection with nested clip-path {self.node_str(clip_geom)}")

                clipped = self.path_intersection(clip_geom, [geom])
                if len(clipped) == 1:
                    geom = clipped[0]
                else:
                    return None

        self.log(logging.DEBUG, f"[CP]     returning geometry {self.node_str(geom)}") 

        return geom


    def normalize_clippath_units(self, flattened:PathElement, cp:ClipPath, shape:BaseElement) -> PathElement:
        """
        Normalize an objectBoundingBox clipPath into userSpaceOnUse by baking the
        shape's bounding box into the clipPath geometry, producing a browser-accurate
        user-space clipping path.

        The conversion proceeds as follows:

        - If clipPathUnits is already userSpaceOnUse, return the flattened geometry
        unchanged.

        - Compute the shape's user-space bounding box transform using
        bbox_transform(). This yields a matrix T_bbox = translate(bx, by) ∘ scale(bw, bh), 
        mapping the unit box [0,1]x[0,1] into the shape's actual bounding box.

        - Apply T_bbox to the flattened clipPath geometry via apply_transform_to_node(),
        producing user-space coordinates that match browser rendering.

        - Remove the clipPathUnits attribute from the <clipPath> so that the
        clipPath is now semantically userSpaceOnUse.

        - Return the transformed geometry. The caller is responsible for ensuring
        that the clipPath contains exactly one <path> child, as required by GT7
        constraints.

        This function performs no unioning or clipping; it strictly converts
        objectBoundingBox units into user-space geometry.
        """

        # 0. Already userSpaceOnUse → nothing to do
        units = cp.get("clipPathUnits", "userSpaceOnUse")
        if units == "userSpaceOnUse":
           return flattened

        cp_id = cp.get("id", "")
        self.log(logging.DEBUG,
                f"[CP] Converting clipPath id={cp_id} from objectBoundingBox → userSpaceOnUse")

        # 1. Compute bounding box in user space
        T_bbox = self.bbox_transform(shape)

        self.log(logging.DEBUG, f"[CP]   T_bbox={T_bbox}")

        # 2. Apply bbox transform to flattened geometry
        _, flattened = self.apply_transform_to_node(flattened, T_bbox)
        
        cp.attrib.pop("clipPathUnits", None)
        
        self.log(logging.DEBUG, f"[CP] clipPath id={cp_id} converted to userSpaceOnUse")

        return flattened


    def resolve_clippath_for_group(self, group:Group) -> int:
        """
        Resolve a <clipPath> applied to a group by flattening the clipPath,
        normalizing its units, intersecting it with each child geometry inside the
        group, and replacing each clipped child in the DOM while preserving
        z-order and presentation attributes.

        The routine performs the following steps:

        - Locate the group's clip-path reference. If none exists, return 0. Remove
        the clip-path attribute from the group so children can be processed
        individually.

        - Resolve the clipPath geometry using resolve_clippath(), producing a
        single flattened user-space path. If empty, stop.

        - Normalize clipPathUnits using normalize_clippath_units(),
        converting objectBoundingBox clipPaths into userSpaceOnUse.

        - Collect all geometry children under the group that carry a clip-path
        attribute (via iter_geometry_subtree with pop_attr=True). Convert each
        to a path and store both the original shape and its path geometry.

        - Intersect the flattened clipPath with each child path using
        path_intersection(). Each clipped result is matched back to its original 
        shape using clipped_path_index().

        - For each clipped shape:
        - Apply the original shape's local transform via
            apply_transform_to_node().
        - Copy presentation attributes from the original shape.
        - Remove id, clip-path, and transform attributes.
        - Insert the clipped geometry at the original shape's position in the
            DOM and remove the original shape.

        - Remove any remaining shapes whose intersection produced empty geometry.

        Returns the number of shapes removed or replaced. This function provides
        browser-accurate group-level clipping, flattening transforms, resolving
        nested clipPaths, and preserving paint-order and styling.
        """

        cp, _ = self.ref_target(group, "clip-path")
        if cp is None:
            return 0

        group.attrib.pop("clip-path", None)

        # 2. Resolve clipPath geometry (two-mode resolver)
        flattened = self.resolve_clippath(cp, Transform())
        if flattened is None:
            # No geometry → remove clip-path
            return 0
        
        self.log(logging.DEBUG, f"flattened geom = {self.node_str(flattened)}")
        flattened = self.normalize_clippath_units(flattened, cp, group)
        self.log(logging.DEBUG, f"normalized geom = {self.node_str(flattened)}")

        shapes = {}
        shape_paths = []

        idx = 0
        for shape, _ in self.iter_geometry_subtree(group, "clip-path", pop_attr=True, only_gt7_geometry=False):
            shapes[idx] = shape
            idx += 1
            shape_paths.append(self.convert_to_path(shape, Transform()))

        clipped_shapes = self.path_intersection(flattened, shape_paths)

        for clipped_shape in clipped_shapes:

            _ , idx = self.clipped_path_index(clipped_shape)

            if not idx is None:
                shape = shapes.get(idx)
                shapes.pop(idx, None)

            if shape is None:
                self.log(logging.WARNING, f"Clipped shape {self.node_str(clipped_shape)} has no matching original shape - ignored")
                continue

            shape_tr = self.local_transform(shape)

            _, clipped_shape = self.apply_transform_to_node(clipped_shape, shape_tr)
            self.copy_presentation_attributes(shape, clipped_shape)
            clipped_shape.attrib.pop("id", None)
            clipped_shape.attrib.pop("clip-path", None)
            clipped_shape.attrib.pop("transform", None)

            parent, idx = self.parent_of(shape)
            self.add_node(clipped_shape, parent, idx)
            self.remove_node(shape, parent)

        #remove any remaining shapes that were intersected into empy geometry
        for shape in shapes.values():
            parent, idx = self.parent_of(shape)
            self.remove_node(shape, parent) 

        return len(shapes)


    def resolve_clippath_for_shape(self, shape:BaseElement) -> int:
        """
        Resolve a shape-level clipPath by flattening the referenced <clipPath>,
        normalizing its units, intersecting it with the shape's geometry, and
        replacing the shape with the clipped result in the DOM while preserving
        presentation attributes and local transforms.

        The routine performs the following steps:

        - Retrieve the structural <clipPath> referenced by the shape. If none
        exists, return 0. Remove the clip-path attribute from the shape so the
        shape can be replaced cleanly.

        - Resolve the clipPath into a single flattened user-space path using
        resolve_clippath(). If empty, stop.

        - Normalize clipPathUnits using normalize_clippath_units(),
        converting objectBoundingBox clipPaths into userSpaceOnUse.

        - Convert the shape to a path in user space and intersect it with the
        flattened clipPath using path_intersection(). If the intersection yields 
        exactly one geometry node, adopt it; otherwise, the shape is removed.

        - Apply the shape's local transform to the clipped geometry via
        apply_transform_to_node(), restore presentation attributes, and remove id, 
        clip-path, and transform attributes.

        - Insert the clipped geometry at the original shape's position in the DOM
        and remove the original shape.

        Returns 1 to indicate that the shape was processed. This function provides
        browser-accurate shape-level clipping with full transform flattening and
        unit normalization.
        """

        # 1. Get structural <clipPath> element
        cp, _ = self.ref_target(shape, "clip-path")
        if cp is None:
            return 0

        shape.attrib.pop("clip-path", None)

        self.log(logging.DEBUG, f"[CP] resolving {self.node_str(cp)} for {self.node_str(shape)}")

        # 2. Resolve clipPath geometry (two-mode resolver)
        flattened = self.resolve_clippath(cp, Transform())
        if flattened is None:
            # No geometry → remove clip-path
            return 0
        
        flattened = self.normalize_clippath_units(flattened, cp, shape)
        self.log(logging.DEBUG, f"[CP] flattened clippath geometry = {self.node_str(flattened)}")

        parent, idx = self.parent_of(shape)
        shape_path = self.convert_to_path(shape, Transform())

        clipped = self.path_intersection(flattened, [shape_path])
        if len(clipped) == 1:
            clipped_shape = clipped[0]
            _, clipped_shape = self.apply_transform_to_node(clipped_shape, self.local_transform(shape))
            self.copy_presentation_attributes(shape, clipped_shape)
            clipped_shape.attrib.pop("id", None)
            clipped_shape.attrib.pop("clip-path", None)
            clipped_shape.attrib.pop("transform", None)
            self.add_node(clipped_shape, parent, idx)

            self.log(logging.DEBUG, f"[CP] clipped shape = {self.node_str(clipped_shape)}")

        else:
            self.log(logging.DEBUG, f"[CP] removing {self.node_str(shape)}")
        
        self.remove_node(shape, parent)

        return 1
        
    # endregion

    # region ---- Inkscape Actions ----
    
    def path_union(self, paths:list[BaseElement]) -> PathElement:
        """
        Union a list of paths into a single PathElement by delegating to Inkscape's
        boolean engine when multiple inputs are present, or by returning a safe
        copy when only one path is provided. All presentation attributes are
        preserved on the returned geometry.

        The routine behaves as follows:

        - If exactly one path is supplied, return a fresh PathElement containing
        the same 'd' attribute and presentation attributes. This avoids mutating
        the caller's original node and keeps the union operation side-effect-free.

        - If multiple paths are supplied:
        - Build a minimal SVG document containing all input paths using
            build_svg_for_actions(), each assigned a unique ID.
        - Invoke Inkscape's boolean union via inkscape_command() on the selected
            IDs. This produces a new SVG containing the union result.
        - Extract the union path by selecting the path whose ID is not one of
            the original operands. If Inkscape replaces rather than appends, fall
            back to the last path in the output.
        - Copy the union's 'd' attribute and presentation attributes into a new
            PathElement so callers can safely mutate the result.

        - If Inkscape returns no geometry, return empty_path().

        This function provides a stable, browser-accurate union operation suitable
        for clipping, pattern tiling, and group flattening, and is used throughout
        the pipeline wherever geometry needs to be merged.
        """

        if len(paths) == 1:
            # Return a *copy* so callers can mutate safely
            single = paths[0]
            new_path = inkex.PathElement()
            new_path.set("d", single.get("d"))
            self.copy_presentation_attributes(single, new_path)
            return new_path

        doc, ids = self.build_svg_for_actions(paths, prefix="u")

        root = doc.getroot()
        svg_input = inkex.etree.tostring(root, encoding="unicode")
        self.log(logging.DEBUG,f"Inkscape input:\n {svg_input}")

        select_ids = ",".join(ids)

        result_bytes = inkex.command.inkscape_command(
            doc,
            select=select_ids,
            actions="path-union",
        )

        result_doc = inkex.load_svg(result_bytes)
        result_root = result_doc.getroot()

        svg_output = inkex.etree.tostring(result_root, encoding="unicode")
        self.log(logging.DEBUG,f"Inkscape ouput:\n {svg_output}")
        out_paths = result_root.findall(".//{http://www.w3.org/2000/svg}path")
        candidates = [p for p in out_paths if p.get("id") not in ids]

        union = candidates[-1] if candidates else (out_paths[-1] if out_paths else None)
        if union is None:
            self.log(logging.DEBUG, f"Epty result set - returning empty_path")
            return self.empty_path()

        new_path = inkex.PathElement()
        new_path.set("d", union.get("d"))
        self.copy_presentation_attributes(union, new_path)
        return new_path


    def compute_union_bbox(self, nodes:list[BaseElement]) -> tuple[float,float]:
        """
        Compute width and height from the union bounding box of a list
        of nodes, temporarily removing clip-path attributes so bounding_box()
        returns the true geometric extent rather than the clipped extent.

        The routine proceeds as follows:

        - Temporarily remove each node's clip-path attribute so that
        bounding_box() evaluates the node's actual geometry instead of its
        clipped shape.

        - Accumulate all bounding boxes into a single union BoundingBox.

        - Restore all clip-path attributes exactly as they were.

        - Return the union width and height, falling back to 1.0x1.0 if the union
        is empty or degenerate.

        This function is used when computing pattern tile sizes prior to tiling or
        viewBox normalization, ensuring that tile geometry reflects real
        unclipped bounds.
        """

        bbox = inkex.BoundingBox()

        # Store original clip-path attributes so we can restore them
        saved_clip_paths = {}

        # --- 1. Temporarily remove clip-path attributes ---
        for node in nodes:
            cp = node.get("clip-path", None)
            if cp is not None:
                saved_clip_paths[node] = cp
                node.attrib.pop("clip-path", None)

        # --- 2. Compute union bounding box ---
        for node in nodes:
            try:
                bbox += node.bounding_box()
            except Exception:
                pass  # non-geometry nodes

        # --- 3. Restore clip-path attributes ---
        for node, cp in saved_clip_paths.items():
            node.set("clip-path", cp)

        # --- 4. Return safe dimensions ---
        width = bbox.width if bbox.width > 0 else 1.0
        height = bbox.height if bbox.height > 0 else 1.0

        return width, height


    def build_svg_for_actions(self, nodes:list[BaseElement], prefix:str | Callable[[int, BaseElement], str], 
                              include_references:bool=False, pop_attrs:str|list[str]|None=None) -> tuple[BaseElement,list[str]]:
        """
        Build a minimal standalone SVG document containing the given nodes so they
        can be passed to Inkscape's Actions API (union, intersection, difference,
        etc.). Each node is cloned, assigned a stable ID, stripped of selected
        attributes, and inserted into a fresh SVG with a tight viewBox sized to
        the union of all node geometry.

        The routine performs the following steps:

        - Normalize pop_attrs into a list and compute the union bounding box of
        all nodes via compute_union_bbox(). The resulting width/height define the 
        standalone SVG's viewBox.

        - Create a minimal <svg> root sized to the union bounds. This ensures
        boolean operations run in a predictable coordinate space.

        - Optionally copy <defs> and other reference-bearing nodes from the
        original document when include_references=True. This allows gradients,
        patterns, markers, and other referenced resources to remain valid during
        boolean operations.

        - For each node:
        - Clone the node and assign an ID using either the prefix string or the
            prefix callback.
        - Copy presentation attributes from the original node.
        - Remove any attributes listed in pop_attrs.
        - Append the clone to the new SVG root.
        - Rebind the clone using root.getElementById() so callers receive the
            correct DOM instance.

        - Return the constructed SVG document and the list of assigned IDs. These
        IDs are used by boolean operations such as path-union, path-intersection,
        and path-difference.

        This function is the foundation for all boolean geometry operations in the
        pipeline and is used by path_union(), path_intersection(), and related
        Actions API calls.
        """

        if pop_attrs is None:
            pop_attrs = []
        elif not isinstance(pop_attrs, (list, tuple)):
            pop_attrs = [pop_attrs]

        width, height = self.compute_union_bbox(nodes)

        minimal_svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}"></svg>'
        )

        doc = inkex.load_svg(minimal_svg)
        root = doc.getroot()

        if include_references:
            defs = self.svg.xpath("//svg:defs", namespaces=inkex.NSS)
            for d in defs:
                clone = copy.deepcopy(d)
                root.append(clone)

            scripts = self.svg.xpath("//svg:defs", namespaces=inkex.NSS)
            for d in scripts:
                clone = copy.deepcopy(d)
                root.append(clone)

        ids = []

        for i, node in enumerate(nodes):
            if not hasattr(node, "copy"):
                continue

            clone = node.copy()

            if callable(prefix):
                elem_id = prefix(i, node)
            else:
                elem_id = f"{prefix}{i}"

            clone.set("id", elem_id)

            self.copy_presentation_attributes(node, clone)
            for attr in pop_attrs:
                clone.attrib.pop(attr, None)

            root.append(clone)

            # IMPORTANT: rebind using root, not doc
            clone = root.getElementById(elem_id)

            ids.append(elem_id)

        return doc, ids


    def rasterize_nodes(self, nodes:list[BaseElement]) -> bytes:
        """
        Rasterize a list of nodes into a PNG by constructing a minimal standalone
        SVG, invoking Inkscape's renderer through the Actions API, and returning
        the raw PNG bytes. Tile size is computed automatically from the union
        geometry of all nodes, ensuring that the rasterization area matches the
        true user-space bounds.

        The routine performs the following steps:

        - Build a standalone SVG containing the nodes using build_svg_for_actions(), 
        with include_references=True so gradients, patterns, and other referenced
        resources remain valid during rasterization.

        - Serialize the temporary SVG and write it to a transient file path that
        Inkscape's renderer can export from.

        - Invoke inkscape_command() with:
        - export-filename → the temporary PNG path  
        - export-type → png  
        - export-dpi → 300  
        - export-area-page → rasterize the entire standalone SVG  
        - export-do → perform the export

        - Read the resulting PNG bytes from disk. If a test directory is active,
        copy the PNG there for debugging.

        - Delete the temporary PNG file and return the PNG bytes.

        This function is no longer used, but kept in the cod for later use.
        """

        # 1) Build SVG input
        doc, _ = self.build_svg_for_actions(nodes, "path", include_references=True)
        
        root = doc.getroot()
        svg_input = inkex.etree.tostring(root, encoding="unicode")
        self.log(logging.DEBUG,f"Inkscape input:\n {svg_input}")

        # 2) Create temp PNG filename
        tmp_png = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp_png_path = tmp_png.name
        tmp_png.close()

        # 3) Rasterize using Inkscape actions API
        inkex.command.inkscape_command(
            doc,
            actions=";".join([f"export-filename:{tmp_png_path}", "export-type:png",
                "export-dpi:300", "export-area-page", "export-do"])
        )

        self.log(logging.DEBUG, f"Rasterizer PNG path: {tmp_png_path}")

        # 4) Read PNG bytes
        with open(tmp_png_path, "rb") as f:
            png_bytes = f.read()

        if self.test_dir and os.path.isdir(self.test_dir):
            filename = os.path.basename(tmp_png_path)
            path = os.path.join(self.test_dir, filename)
            self.log(logging.DEBUG, f"Coyping image to {path}")
            shutil.copy(tmp_png_path, path)

        os.remove(tmp_png_path)

        self.log(logging.DEBUG,f"Read {len(png_bytes)} bytes")

        return png_bytes


    def actions_for_multi_path_intersection(self, cp_ids:list[str], path_ids:list[str]) -> str:
        """
        Build an Inkscape Actions API command sequence that performs a batch of
        pairwise path intersections, each of the form:

            cp0 ∩ p0 → r0
            cp1 ∩ p1 → r1
            cp2 ∩ p2 → r2
            ...

        The sequence is constructed by iterating over corresponding clipPath IDs
        and path IDs. For each pair, the following Actions are appended:

        - select-by-id:<path>       — select the path operand
        - select-by-id:<clipPath>   — add the clipPath operand to the selection
        - path-intersection         — perform the boolean intersection
        - select-clear              — clear the selection before the next pair

        The resulting string is a semicolon-delimited Actions chain suitable for a
        single Inkscape invocation. Each intersection produces a new result path
        whose ID is assigned by Inkscape. Callers typically identify the results
        later using their own ID-mapping logic (e.g., clipped_path_index).

        This function is used by multi-element clipping pipelines such as
        path_intersection() and resolve_clippath_for_group(), allowing N independent
        intersections to be executed efficiently in one Actions call.
        """

        actions = []

        for cp_id, pid in zip(cp_ids, path_ids):
            actions.append(f"select-by-id:{pid}")
            actions.append(f"select-by-id:{cp_id}")
            actions.append("path-intersection")
            actions.append("select-clear")

        return ";".join(actions)

    def strokes_to_paths(self, doc:BaseElement, stroke_ids:list[str]) -> BaseElement:

        root = doc.getroot()
        self.log_svg(root, header="Inkscape Input")

        actions = []

        for s_id in stroke_ids:
            # 1. Clear selection and run conversion
            actions.append("select-clear")
            actions.append(f"select-by-id:{s_id}")
            actions.append("object-stroke-to-path")
            
            # 2. Break group wrapper (If Inkscape created one)
            actions.append("selection-ungroup")
            
            # 3. Tag whatever is currently highlighted with the loop class
            actions.append(f"object-set-attribute:class,class-{s_id}")
            actions.append("select-clear")

        actions_str = ";".join(actions)

        self.log(logging.DEBUG, f"Actions:\n{';\n'.join(actions_str.split(';'))};")
        
        # --- 3. Run Inkscape once ---
        result_bytes = inkex.command.inkscape_command(
            doc,
            actions=actions_str,
        )

        # --- 4. Parse result ---
        result_doc = inkex.load_svg(result_bytes)
        result_root = result_doc.getroot()

        self.log_svg(result_root, header="Inkscape Output")

        self.resolve_styles_to_attributes(result_root)

        # --- 5. Extract all <path> elements ---
        out_paths = result_root.findall(".//{http://www.w3.org/2000/svg}path")

        for p in out_paths:
            self.log(logging.DEBUG, f"Iterating {self.node_str(p)}")

            class_name = p.get("class", None)
            if class_name is None:
                self.log(logging.DEBUG, f"Ignoring {self.node_str(p)}")
                continue

            fill = p.get("fill", "none")

            if fill == "none":
                self.log(logging.DEBUG, f"Removing {self.node_str(p)}")
                self.remove_node(p)

            original_id = class_name.removeprefix("class-")
            p.set("id", original_id)
            p.attrib.pop("class", None)
            self.log(logging.DEBUG, f"Retaining {self.node_str(p)}")
                
        return result_doc


    def svg_for_path_intersection(self, cp_list:list[BaseElement], paths_to_clip:list[list[BaseElement]]):
        """
        Build an SVG document for multi-batch path intersection.

        Each batch contains one clip path and one or more tile paths. The function
        assigns stable IDs, collects all nodes into a temporary SVG document, and
        returns the document along with ID mappings for downstream intersection
        processing.

        ID scheme:
            cpN  — clip path for batch N
            NpK — tile path K in batch N

        Processing steps:

        1. Validate input.
        The number of clip paths must match the number of path batches.

        2. Assign IDs and collect nodes.
        Tile paths are deep-copied and assigned IDs of the form "NpK".
        Clip paths are deep-copied and assigned IDs of the form "cpN".
        Tile paths representing strokes are recorded in a stroke-ID set.

        3. Build SVG document.
        All collected nodes are passed to the SVG builder to produce a temporary
        document containing clip paths and tile paths with their assigned IDs.

        4. Convert stroked paths.
        Any tile paths identified as strokes are converted into filled paths to
        ensure correct boolean intersection behavior.

        Returns:
            doc (BaseElement):
                The constructed SVG document containing all clip paths and tile paths.

            cp_ids (list[str]):
                Clip-path IDs ("cpN"), one per batch.

            path_ids (list[str]):
                Tile-path IDs ("NpK") across all batches.

            stroke_ids (set[str]):
                IDs corresponding to tile paths that represent strokes.

        """


        assert len(cp_list) == len(paths_to_clip)

        all_nodes = []
        cp_ids = []          # ["cp0", "cp1", ...]
        path_ids = []        # ["0p0", "0p1", "1p0", ...]
        stroke_ids = set()

        for idx in range(len(cp_list)):
            path_list = paths_to_clip[idx]

            # --- tile paths ---
            for tile_index, p in enumerate(path_list):
                p_copy = p.copy()
                pid = f"{idx}p{tile_index}"
                p_copy.set("id", pid)
                all_nodes.append(p_copy)
                path_ids.append(pid)

                if self.is_stroke(p_copy):
                    stroke_ids.add(pid)

                self.log(logging.DEBUG, f"Adding path {pid}")

                # --- cp: exactly one per batch ---
                cp = cp_list[idx]
                cp_copy = cp.copy()
                cp_id = f"{idx}cp{tile_index}"
                cp_copy.set("id", cp_id)
                all_nodes.append(cp_copy)
                cp_ids.append(cp_id)

                self.log(logging.DEBUG, f"Adding clippath {cp_id}")

        # Build SVG using your existing builder
        doc, _ = self.build_svg_for_actions(
            all_nodes,
            prefix=lambda i, node: node.get("id") or "",
            include_references=True
        )

        self.log(logging.DEBUG, f"stroke_ids={stroke_ids}")

        if len(stroke_ids) > 0:
            doc = self.strokes_to_paths(doc, list(stroke_ids))

        return doc, cp_ids, path_ids, stroke_ids


    def clipped_path_index(self, node):
        """
        Extract the clipPath and path indices encoded in a result path's ID.

        This function is used after performing multi-path intersections, where
        Inkscape generates new result paths whose IDs encode both operands. The
        expected ID format is:

            "<cp_idx>p<p_idx>"

        Examples:
            "0p3"   → cp_idx = 0, p_idx = 3
            "12p7"  → cp_idx = 12, p_idx = 7
            "p4"    → cp_idx = 0, p_idx = 4   (implicit cp index)

        The function attempts to parse this pattern using a regular expression.
        If the ID matches, it returns a tuple (cp_idx, p_idx). If the ID does not
        match the expected format or the node has no ID, it returns (None, None).

        This helper is typically used to map each clipped result back to the
        original clipPath and geometry operand during multi-element clipping.
        """

        id:str = node.get("id", None)
        if id is None:
            return None, None

        # Extract index from ID
        m = re.match(r"(\d+)?p(\d+)", id)
        if m:
            cp = m.group(1)
            cp_idx = int(cp) if cp is not None else 0
            p_idx = int(m.group(2))
            return cp_idx, p_idx

        return None, None


    @overload
    def path_intersection(
        self,
        cp_list: BaseElement,
        paths_to_clip: Sequence[BaseElement]
    ) -> List[inkex.PathElement]:
        ...

    @overload
    def path_intersection(
        self,
        cp_list: Sequence[BaseElement],
        paths_to_clip: Sequence[List[BaseElement]]
    ) -> List[List[PathElement]]:
        ...

    def path_intersection(self, cp_list, paths_to_clip) :
        """
        Perform multi-path intersection using a single Inkscape invocation.

        This method supports two calling modes:

        **1. Single clip path + 1-D list of paths**

            cp_list: BaseElement  
            paths_to_clip: list[BaseElement]

            Returns:
                list[inkex.PathElement]

            The result list contains the clipped versions of each path.  
            If all paths are fully clipped away, the returned list is empty.

        **2. Multiple clip paths + 2-D list of path batches**

            cp_list: list[BaseElement]  
            paths_to_clip: list[list[BaseElement]]

            Returns:
                list[list[inkex.PathElement]]

            Each row corresponds to one clip path.  
            Rows whose paths were fully clipped are returned as empty lists.

        The function internally normalizes both calling modes into a uniform
        2-D structure, builds a temporary SVG document containing all clip
        paths and target paths, and executes Inkscape's boolean intersection
        operator once. The resulting `<path>` elements are then extracted,
        matched back to their originating inputs, and grouped according to
        the original structure.

        Processing steps:

        - Normalize input into lists of clip paths and path batches.
        - Construct an SVG document with unique IDs for all nodes.
        - Build an Inkscape action chain for multi-intersection.
        - Run Inkscape once to compute all intersections.
        - Parse the resulting SVG and extract all `<path>` elements.
        - Reassign presentation attributes from the source nodes.
        - Convert stroked intersections into filled paths when needed.
        - Reassemble results into either a 1-D or 2-D list depending on
        the calling mode.

        Args:
            cp_list (BaseElement | list[BaseElement]):
                A single clip path or a list of clip paths.
            paths_to_clip (list[BaseElement] | list[list[BaseElement]]):
                Paths to intersect with the clip path(s).

        Returns:
            list[inkex.PathElement] | list[list[inkex.PathElement]]:
                Clipped path results, either as a flat list (single clip path)
                or a 2-D list (multiple clip paths).
        """
        single_cp = not isinstance(cp_list, (list, tuple))

        if single_cp:
            assert isinstance(paths_to_clip, list)
        else:
            assert all(isinstance(row, list) for row in paths_to_clip)

        if single_cp:
            cp_list = [cp_list]
            paths_to_clip = [paths_to_clip]
        else:
            cp_list = list(cp_list)
            paths_to_clip = [list(row) for row in paths_to_clip]

        # --- 1. Build SVG with cpN + NpK IDs ---
        doc, cp_ids, path_ids, stroke_ids = \
            self.svg_for_path_intersection(cp_list, paths_to_clip)

        root = doc.getroot()
        self.log_svg(root, header="Inkscape Input")

        # --- 2. Build actions chain ---
        actions = self.actions_for_multi_path_intersection(
            cp_ids,
            path_ids
        )

        self.log(logging.DEBUG, f"Actions:\n{';\n'.join(actions.split(';'))};")

        # --- 3. Run Inkscape once ---
        result_bytes = inkex.command.inkscape_command(
            doc,
            actions=actions,
        )

        # --- 4. Parse result ---
        result_doc = inkex.load_svg(result_bytes)
        result_root = result_doc.getroot()

        self.log_svg(result_root, header="Inkscape Output")

        # --- 5. Extract all <path> elements ---
        out_paths = result_root.findall(".//{http://www.w3.org/2000/svg}path")

        # Prepare empty batches
        count = len(paths_to_clip)
        clipped_paths = [[] for _ in range(count)]

        # Iterate over all result paths
        for p in out_paths:
            pid_str = p.get("id")

            if pid_str is None:
                self.log(logging.DEBUG, f"Path without ID in output: {pid_str}") 
                continue

            # Parse "<cp_id>p<p_id>"
            cp_idx, p_idx = self.clipped_path_index(p)
            if cp_idx is None or p_idx is None:
                continue

            src_node = paths_to_clip[cp_idx][p_idx]
            self.copy_presentation_attributes(src_node, p)

            if pid_str in stroke_ids:
                fill = src_node.get("stroke", "none")
                p.attrib.pop("stroke", None)
                p.attrib.pop("stroke-width", None)
                p.set("fill", fill)

            # Append to correct batch
            clipped_paths[cp_idx].append(p)

        if single_cp:
            return clipped_paths[0]

        return clipped_paths

    
    # endregion

# region --- Main ---

if __name__ == "__main__":
    GT7Output().run()

# endregion

#region --- MeshPatch class ---

# ------------------------------------------------------------
# Basic geometry
# ------------------------------------------------------------
@dataclass
class Point:
    """A 2D point with basic vector operations.

    Attributes:
        x: Horizontal coordinate.
        y: Vertical coordinate.
    """

    x: float
    y: float

    def add(self, other: "Point") -> "Point":
        """Return the vector sum of this point and another.

        Args:
            other: The point to add.

        Returns:
            A new Point whose coordinates are the sum of both points.
        """
        return Point(self.x + other.x, self.y + other.y)

    def scale(self, s: float) -> "Point":
        """Return a uniformly scaled version of this point.

        Args:
            s: Scale factor applied to both coordinates.

        Returns:
            A new Point with coordinates scaled by `s`.
        """
        return Point(self.x * s, self.y * s)


class ColorNode(TypedDict):

    """
    A mesh gradient control node containing position, color, and
    directional color derivatives.

    Attributes:
        point: The node's position in user space.
        color: RGBA color at this node, as a 4‑tuple of floats.
        du: Partial derivative of the color with respect to U.
        dv: Partial derivative of the color with respect to V.
    """ 

    point: Point                                
    color: tuple[float, float, float, float]    
    du: tuple[float, float, float, float]       
    dv: tuple[float, float, float, float]       

class MeshGradientEvaluator:
    """Evaluate colors inside an SVG mesh gradient.

    This class replicates the logic of Inkscape's fallback JavaScript
    renderer for mesh gradients. Inkscape uses that script to rasterize
    mesh gradients when native support is unavailable. Instead of producing
    a bitmap, this class provides a programmatic way to compute the color
    at any point inside the mesh.

    The outer pipeline uses this evaluator to sample colors on triangulated
    patches and construct equivalent linear gradients that approximate the
    original mesh gradient.
    """

    """Coefficient matrix used for bicubic interpolation in mesh gradient
    evaluation.

    This 16x16 matrix transforms the 16 control values of a mesh patch
    (color components and their directional derivatives) into the polynomial
    coefficients of the bicubic surface. It is derived from the same basis
    functions used by Inkscape's fallback mesh-gradient renderer and matches
    the structure of the matrix found in Inkscape's JavaScript implementation.

    The matrix is applied to a 16-element vector representing the control
    values at a patch corner, producing another 16-element vector containing
    the coefficients of the bicubic polynomial evaluated by the mesh
    gradient sampler.
    """
    U_MATRIX: list[list[float]] = [
        [1,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0],
        [0,0,0,0, 1,0,0,0, 0,0,0,0, 0,0,0,0],
        [-3,3,0,0, -2,-1,0,0, 0,0,0,0, 0,0,0,0],
        [2,-2,0,0, 1,1,0,0, 0,0,0,0, 0,0,0,0],

        [0,0,0,0, 0,0,0,0, 1,0,0,0, 0,0,0,0],
        [0,0,0,0, 0,0,0,0, 0,0,0,0, 1,0,0,0],
        [0,0,0,0, 0,0,0,0, -3,3,0,0, -2,-1,0,0],
        [0,0,0,0, 0,0,0,0, 2,-2,0,0, 1,1,0,0],

        [-3,0,3,0, 0,0,0,0, -2,0,-1,0, 0,0,0,0],
        [0,0,0,0, -3,0,3,0, 0,0,0,0, -2,0,-1,0],
        [9,-9,-9,9, 6,3,-6,-3, 6,-6,3,-3, 4,2,2,1],
        [-6,6,6,-6, -3,-3,3,3, -4,4,-2,2, -2,-2,-1,-1],

        [2,0,-2,0, 0,0,0,0, 1,0,1,0, 0,0,0,0],
        [0,0,0,0, 2,0,-2,0, 0,0,0,0, 1,0,1,0],
        [-6,6,6,-6, -4,-2,4,2, -3,3,-3,3, -2,-1,-2,-1],
        [4,-4,-4,4, 2,2,-2,-2, 2,-2,2,-2, 1,1,1,1],
    ]

    def __init__(self, mesh_el: MeshGradient, shape:BaseElement, outer:GT7Output):
        """Initialize all data structures required for mesh-gradient evaluation.

        Reads the <meshgradient> element, extracts control points and colors,
        computes the mesh's bounding box in the coordinate space of the target
        shape, and constructs the color lattice used for bilinear or bicubic
        interpolation.

        Args:
            mesh_el: The <meshgradient> element containing rows, columns, control
                points, and color stops.
            shape: The SVG element whose geometry defines the sampling region for
                the mesh gradient.
            outer: The outer processing context providing transforms, logging, and
                shape-related utilities.

        Initializes:
            type: Mesh type ("bilinear" or "bicubic").
            nodes: 2D list of control points (Point or None).
            colors: 2D list of RGBA tuples (or None).
            color_lattice: 2D list of ColorNode entries used for interpolation.
            mesh_transform: Transform applied to mesh coordinates.
            xmin, ymin, xmax, ymax: Mesh bounds in user space.
            bbox: Bounding box tuple (xmin, ymin, xmax, ymax).
            outer: Reference to the outer processing context.

        The mesh is fully parsed and the color lattice is constructed during
        initialization.
        """

        self.type = mesh_el.get("type") or "bilinear"
        self.nodes: list[list[Optional[Point]]] = []
        self.colors: list[list[Optional[tuple[float,float,float,float]]]] = []
        self.color_lattice: list[list[ColorNode]] = []

        self.mesh_transform:Transform = Transform()
        self.xmin:float = 0
        self.ymin:float = 0
        self.xmax:float = 0
        self.ymax:float = 0
        self.bbox:tuple[float,float,float,float] = (0,0,1,1)
        self.outer:GT7Output = outer

        self._read_mesh(mesh_el, outer.shape_bbox(shape))
        self._build_color_lattice()


    def _read_mesh(self, mesh_el: MeshGradient, bbox:tuple[float,float,float,float]|None):
        """Parse the <meshgradient> element and populate geometry and color lattices.

        This method reads all mesh rows, patches, control points, and color stops
        from the <meshgradient> element. It applies gradientUnits and
        gradientTransform, computes the mesh's coordinate system relative to the
        shape's bounding box, and constructs the raw node and color grids used
        later for interpolation.

        Args:
            mesh_el: The <meshgradient> element containing mesh rows, patches,
                control points, and color stops.
            bbox: Optional bounding box of the target shape in user space. If
                provided, it is used to interpret gradientUnits and to normalize
                mesh coordinates.

        Behavior:
            - Applies gradientTransform if present.
            - Interprets gradientUnits ("userSpaceOnUse" or "objectBoundingBox")
            and applies the appropriate coordinate transform.
            - Reads the mesh origin (x, y) and initializes the node and color
            grids.
            - Iterates through each <meshrow> and <meshpatch>, parsing stop
            geometry and color information.
            - Applies edge commands (l, L, c, C) to build control point geometry.
            - Computes interior control points for bicubic patches.
            - Logs geometry and color grids for debugging.
            - Applies the final mesh transform to all points.
            - Computes mesh bounds (xmin, xmax, ymin, ymax) in user space.

        The method fully constructs the raw mesh geometry and color arrays that
        later feed into the color-lattice builder and interpolation routines.
        """

        if bbox is not None:
            self.bbox = bbox
            
        bx, by, bw, bh = self.bbox

        self.mesh_transform = Transform()

        gt = mesh_el.get("gradientTransform")
        self.outer.log(logging.DEBUG, f"gradientTransform = {gt}")

        if gt:
            try:
                self.mesh_transform = Transform(gt)
            except Exception as ex:
                self.outer.log(logging.DEBUG,f"Invalid gradientTransform: {ex}")

        units = mesh_el.get("gradientUnits") or "objectBoundingBox"

        if units == "userSpaceOnUse":
                bbox_transform = Transform(f"translate({-bx},{-by})")
                self.outer.log(logging.DEBUG, f"bboxTransform = {bbox_transform}")
                self.mesh_transform = self.mesh_transform @ bbox_transform

        else:
            units_transform = Transform(f"scale(new Point({bw}, {bh})")
            self.outer.log(logging.DEBUG, f"unitTransform = {units_transform}")
            self.mesh_transform = self.mesh_transform @ units_transform
            
        # origin
        x0 = float(mesh_el.get("x") or 0.0)
        y0 = float(mesh_el.get("y") or 0.0)

        self.nodes = [[Point(x0, y0)]]
        self.colors = [[None]]
            
        meshrows = list(mesh_el)  # <meshrow> children

        for t, row_el in enumerate(meshrows):
            # ensure geometry rows for this meshrow
            while len(self.nodes) <= 3 * t + 3:
                self.nodes.append([])

            # ensure color row
            while len(self.colors) <= t + 1:
                self.colors.append([])

            meshpatches = list(row_el)  # <meshpatch> children

            for n, patch_el in enumerate(meshpatches):
                stops = list(patch_el)  # <stop> children

                for r_idx, stop_el in enumerate(stops):
                    i = r_idx
                    if t != 0:
                        i += 1  # JS offset

                    path = stop_el.get("path")
                    cmd = "l"
                    coords: List[Point] = []
                    if path is not None:
                        m = re.match(r"\s*([lLcC])\s*(.*)", path)
                        if m:
                            cmd = m.group(1)
                            coords = self._parse_coords(m.group(2))

                    self._apply_edge(t, n, i, cmd, coords)
                    self._apply_color(t, n, i, stop_el)

                # after all stops for this patch: compute interior control points
                self._compute_interior(t, n)

                # ⭐ LOG PATCH COLOR GRID
                try:
                    c00 = self.colors[t][n]
                    c10 = self.colors[t][n+1]
                    c01 = self.colors[t+1][n]
                    c11 = self.colors[t+1][n+1]

                    self.outer.log(
                        logging.DEBUG,
                        f"[GRID] Patch ({t},{n}) colors: "
                        f"c00={c00}, c10={c10}, c01={c01}, c11={c11}"
                    )
                except Exception as ex:
                    self.outer.log(
                        logging.DEBUG,
                        f"[GRID] Patch ({t},{n}) incomplete: {ex}"
                    )

        for r, row in enumerate(self.nodes):
            for c, pt in enumerate(row):
                x = None; y = None
                if pt is not None:
                    x = pt.x; y = pt.y

                self.outer.log(logging.DEBUG,
                    f"[GEOM] node[{r}][{c}] = ({x}, {y})"
                )

        for r_idx, row in enumerate(self.colors):
                    for c_idx, rgba in enumerate(row):
                        r = None
                        b = None
                        g = None
                        a = None
                        if rgba is not None:
                            (r,g,b,a) = rgba
        
                        self.outer.log(logging.DEBUG,
                            f"[COLOR] color[{r_idx}][{c_idx}] = ({r}, {g}, {b}, {a})"
                        )

        points: list[Point] = []

        for row in self.nodes:
            for pt in row:
                assert pt is not None
                points.append(pt)

        self._apply_mesh_transform()            

        self.xmin = min(pt.x for pt in points)
        self.xmax = max(pt.x for pt in points)
        self.ymin = min(pt.y for pt in points)
        self.ymax = max(pt.y for pt in points)

        self.outer.log(logging.DEBUG, f"[GEOM] shape bbox: x_min={bx:.3f}, y_min={by:.3f}, x_max={bx+bw:.3f}, y_max={by+bh:.3f}")
        self.outer.log(logging.DEBUG, f"[GEOM] mesh bounds: x_min={self.xmin:.3f}, y_min={self.ymin:.3f}, x_max={self.xmax:.3f}, y_max={self.ymax:.3f}")


    """Apply the mesh gradient transform to all control points.

    This updates every non-null point in the mesh's node grid by applying the
    final mesh_transform, which incorporates gradientTransform, gradientUnits,
    and any bounding-box normalization. After this step, all mesh coordinates
    are expressed in the target shape's user space.

    The transform is applied in place, modifying each Point's x and y values.
    """
    def _apply_mesh_transform(self) -> None:
        for row in self.nodes:
            for pt in row:
                if pt is None:
                    continue

                x, y = self.mesh_transform.apply_to_point((pt.x, pt.y))

                pt.x = x
                pt.y = y

    def _build_color_lattice(self) -> None:
        """Construct the color lattice used for bilinear or bicubic interpolation.

        This builds a grid of ColorNode entries, each containing a point location,
        its RGBA color, and the directional color derivatives du and dv. The
        lattice is derived from the raw mesh control points and colors parsed
        earlier.

        Behavior:
            - Initializes a lattice entry for each color cell, mapping it to the
            corresponding geometric control point.
            - Computes du and dv derivatives for interior nodes using neighboring
            colors and distances.
            - Extrapolates du and dv for boundary nodes to ensure smooth behavior
            at the edges of the mesh.
            - Produces a complete 2D lattice of ColorNode objects, each containing:
                point: Position in user space.
                color: RGBA tuple at that node.
                du: Color derivative along the U direction.
                dv: Color derivative along the V direction.

        The resulting lattice provides the directional color information required
        to evaluate bicubic or bilinear mesh-gradient patches.
        """

        self.color_lattice = []

        rows = len(self.colors)
        cols = len(self.colors[0])

        zero = (0.0, 0.0, 0.0, 0.0)

        for r in range(rows):
            lattice_row: list[ColorNode] = []

            for c in range(cols):
                node: ColorNode = {
                    "point": self._point(3 * r, 3 * c),
                    "color": self._color(r, c),
                    "du": zero,
                    "dv": zero,
                }

                lattice_row.append(node)

            self.color_lattice.append(lattice_row)

        rows = len(self.color_lattice)
        cols = len(self.color_lattice[0])

        for r in range(1, rows - 1):
            for c in range(1, cols - 1):

                node = self.color_lattice[r][c]

                dist_left = self._distance(
                    self.color_lattice[r - 1][c]["point"],
                    node["point"],
                )

                dist_right = self._distance(
                    self.color_lattice[r + 1][c]["point"],
                    node["point"],
                )

                node["du"] = self._compute_derivative(
                    self.color_lattice[r - 1][c]["color"],
                    node["color"],
                    self.color_lattice[r + 1][c]["color"],
                    dist_left,
                    dist_right,
                )

                dist_left = self._distance(
                    self.color_lattice[r][c - 1]["point"],
                    node["point"],
                )

                dist_right = self._distance(
                    self.color_lattice[r][c + 1]["point"],
                    node["point"],
                )

                node["dv"] = self._compute_derivative(
                    self.color_lattice[r][c - 1]["color"],
                    node["color"],
                    self.color_lattice[r][c + 1]["color"],
                    dist_left,
                    dist_right,
                )

        #
        # boundary du derivatives
        #
        last_row = rows - 1

        for c in range(cols):

            dist = self._distance(
                self.color_lattice[1][c]["point"],
                self.color_lattice[0][c]["point"],
            )

            self.color_lattice[0][c]["du"] = self._extrapolate_derivative(
                self.color_lattice[0][c]["color"],
                self.color_lattice[1][c]["color"],
                self.color_lattice[1][c]["du"],
                dist,
            )

            dist = self._distance(
                self.color_lattice[last_row][c]["point"],
                self.color_lattice[last_row - 1][c]["point"],
            )

            self.color_lattice[last_row][c]["du"] = self._extrapolate_derivative(
                self.color_lattice[last_row][c]["color"],
                self.color_lattice[last_row - 1][c]["color"],
                self.color_lattice[last_row - 1][c]["du"],
                dist,
            )

        #
        # boundary dv derivatives
        #
        last_col = cols - 1

        for r in range(rows):

            dist = self._distance(
                self.color_lattice[r][1]["point"],
                self.color_lattice[r][0]["point"],
            )

            self.color_lattice[r][0]["dv"] = self._extrapolate_derivative(
                self.color_lattice[r][0]["color"],
                self.color_lattice[r][1]["color"],
                self.color_lattice[r][1]["dv"],
                dist,
            )

            dist = self._distance(
                self.color_lattice[r][last_col]["point"],
                self.color_lattice[r][last_col - 1]["point"],
            )

            self.color_lattice[r][last_col]["dv"] = self._extrapolate_derivative(
                self.color_lattice[r][last_col]["color"],
                self.color_lattice[r][last_col - 1]["color"],
                self.color_lattice[r][last_col - 1]["dv"],
                dist,
            )


    def _extrapolate_derivative(
        self,
        edge_color: tuple[float, float, float, float],
        inner_color: tuple[float, float, float, float],
        inner_derivative: tuple[float, float, float, float],
        dist: float,
    ) -> tuple[float, float, float, float]:
        """Extrapolate a directional color derivative for a boundary node.

        This computes a derivative at the mesh boundary using the inner node's
        color and derivative. The formula mirrors the behavior of Inkscape's
        mesh-gradient fallback, ensuring smooth transitions at the edges of the
        mesh.

        Args:
            edge_color: Color at the boundary node.
            inner_color: Color at the adjacent interior node.
            inner_derivative: Derivative at the interior node along the same axis.
            dist: Distance between the boundary and interior nodes.

        Returns:
            A 4-tuple representing the extrapolated RGBA derivative. If the
            distance is zero or negative, a zero derivative is returned.
        """

        if dist <= 0:
            return (0.0, 0.0, 0.0, 0.0)

        out = [0.0, 0.0, 0.0, 0.0]

        for channel in range(4):
            out[channel] = (
                2.0 * (inner_color[channel] - edge_color[channel]) / dist
                - inner_derivative[channel]
            )

        return (
            out[0],
            out[1],
            out[2],
            out[3],
        )


    def _compute_derivative(
        self,
        left: tuple[float, float, float, float],
        mid: tuple[float, float, float, float],
        right: tuple[float, float, float, float],
        dist_left: float,
        dist_right: float,
    ) -> tuple[float, float, float, float]:
        """Compute a directional color derivative for an interior lattice node.

        This estimates the derivative along one axis (U or V) using the colors of
        the left, middle, and right nodes and their respective distances. The
        formula mirrors Inkscape's mesh-gradient fallback logic and clamps overly
        large derivatives to avoid overshoot artifacts.

        Args:
            left: Color at the previous node along the axis.
            mid: Color at the current node.
            right: Color at the next node along the axis.
            dist_left: Distance between the left and middle nodes.
            dist_right: Distance between the middle and right nodes.

        Returns:
            A 4-tuple containing the RGB derivative and a zero alpha derivative.
            The alpha channel is always set to 0.0 because the original JS
            implementation only computes derivatives for RGB.
        """

        out = [0.0, 0.0, 0.0, 0.0]

        for h in range(3):  # JS only computes RGB

            t = left[h]
            e = mid[h]
            s = right[h]

            if (e < t and e < s) or (t < e and s < e):
                out[h] = 0.0
            else:
                value = 0.5 * (
                    (e - t) / dist_left +
                    (s - e) / dist_right
                )

                o = abs((3.0 * (e - t)) / dist_left)
                i = abs((3.0 * (s - e)) / dist_right)

                if value > o:
                    value = o
                elif value > i:
                    value = i

                out[h] = value

        return (out[0], out[1], out[2], 0.0)

    def _distance(self, a: Point, b: Point) -> float:
        """Return the Euclidean distance between two points.

        Computes the straight-line distance between points `a` and `b` using
        their x and y coordinates.

        Args:
            a: First point.
            b: Second point.

        Returns:
            The Euclidean distance between the two points.
        """

        return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5


    def _lattice_corners(self, t: int, n: int) -> tuple[ColorNode, ColorNode, ColorNode, ColorNode]:
        """Return the four ColorNode corners of a mesh patch.

        Given a patch index (t, n), this retrieves the corresponding four lattice
        nodes arranged in the standard mesh-gradient order:

            (t, n)       → top-left
            (t, n + 1)   → top-right
            (t + 1, n)   → bottom-left
            (t + 1, n + 1) → bottom-right

        These nodes provide the color and derivative information required for
        bilinear or bicubic interpolation over the patch.
        """

        return (
            self.color_lattice[t][n],
            self.color_lattice[t][n + 1],
            self.color_lattice[t + 1][n],
            self.color_lattice[t + 1][n + 1],
        )

    def dominant_color(self, sample_count:float=64) -> tuple[float, float, float, float]:
        """
        Estimate a representative (dominant) color for the mesh gradient by
        sampling its color field at multiple points across the gradient's
        bounding box.

        This method is intended for fallback scenarios where a mesh gradient
        cannot be applied directly (e.g., strokes referencing mesh gradients,
        where triangulation and clipping produce degenerate or empty patches).
        Instead of attempting geometric subdivision, the gradient is probed
        at a uniform grid of sample locations. Each sample is resolved through
        the evaluator's existing patch lookup and interpolation pipeline.

        The returned color is the arithmetic mean of all successfully sampled
        RGBA values. Because mesh gradients are smooth by construction, a
        coarse grid (e.g., 8x8 samples) provides a stable and visually
        meaningful approximation of the gradient's overall appearance.

        Parameters
        ----------
        sample_count : int
            Total number of samples to take across the gradient domain.
            Must be a perfect square (e.g., 16, 25, 36, 49, 64). The square
            root defines the grid resolution.

        Returns
        -------
        tuple[float, float, float, float]
            The averaged RGBA color in normalized 0-1 range. RGB (color)
            are scaled 0..255, A (alpha) 0..1. If no samples can be resolved 
            (e.g., malformed mesh), returns (0, 0, 0, 0).
        """

        # 1. Determine sampling grid
        n = int(sample_count ** 0.5)
        xs = [i / (n - 1) for i in range(n)]
        ys = [i / (n - 1) for i in range(n)]

        colors = []

        # 2. Sample mesh gradient
        for y in ys:
            for x in xs:
                col = self.color_at_point(x, y)
                if col is not None:
                    colors.append(col)

        if not colors:
            return (0.0, 0.0, 0.0, 0.0)

        # 3. Average RGBA
        r = sum(c[0] for c in colors) / len(colors)
        g = sum(c[1] for c in colors) / len(colors)
        b = sum(c[2] for c in colors) / len(colors)
        a = sum(c[3] for c in colors) / len(colors)

        return (r, g, b, a)


    def color_at_point(self, x: float, y: float) -> tuple[float, float, float, float]:
        """Return the RGBA color at a given point in user space.

        This samples the mesh gradient at coordinates (x, y). The point is first
        normalized into the mesh's local coordinate system using the mesh's
        bounding box. The method then identifies which mesh patch contains the
        point, computes the corresponding (u, v) parameters, and evaluates either
        the bilinear or bicubic interpolant depending on the mesh type.

        Args:
            x: X-coordinate in user space.
            y: Y-coordinate in user space.

        Returns:
            A 4-tuple representing the RGBA color at the given position. RGB (color)
            are scaled 0..255, A (alpha) 0..1. If the point lies outside all mesh patches, 
            a fully transparent black color (0, 0, 0, 0) is returned.
        """

        bx, by, _, _ = self.bbox

        x -= bx
        y -= by

        # 1. Find patch
        patch = self._find_patch(x, y)
        
        if patch is None:
            self.outer.log(logging.DEBUG, f"[COLOR] patch not found → black")
            return (0.0, 0.0, 0.0, 0.0)

        t, n = patch

        # 2. Compute (u, v)
        if self.type == "bilinear":
            u, v = self._bilinear_uv(t, n, x, y)
            color = self._bilinear_color(t, n, u, v)

        else:  # bicubic
            u, v = self._bicubic_uv(t, n, x, y)
            color = self._bicubic_color(t, n, u, v)

        self.outer.log(logging.DEBUG, f"[COLOR] ({x:.2f},{y:.2f}) → {color}")
                       
        return color
    

    def _color(self, row:int, col:int) -> tuple[float,float,float,float]:
        """Return the RGBA color stored at the given lattice position.

        Looks up the color at (row, col) in the mesh's color grid. If the entry is
        defined, the stored RGBA tuple is returned. If the color is missing, an
        IndexError is raised to signal an incomplete or malformed mesh definition.

        Args:
            row: Row index in the color grid.
            col: Column index in the color grid.

        Returns:
            The RGBA tuple at the specified position.

        Raises:
            IndexError: If the color entry is undefined.
        """

        c = self.colors[row][col]
        
        if c is not None:
            return c
        
        raise IndexError( f"color[{row}, {col}] is undefined")


    def _point(self, row:int, col:int) -> Point:
        """Return the geometric control point at the given lattice position.

        Looks up the point stored at (row, col) in the mesh's node grid. If the
        entry is defined, the Point instance is returned. If the point is missing,
        an IndexError is raised to indicate an incomplete or malformed mesh
        definition.

        Args:
            row: Row index in the node grid.
            col: Column index in the node grid.

        Returns:
            The Point object at the specified position.

        Raises:
            IndexError: If the point entry is undefined.
        """

        pt = self.nodes[row][col]

        if pt is not None:
            return pt
        
        raise IndexError( f"point[{row}, {col}] is undefined")

    def _points(self, row: int, from_column: int, to_column: int) -> tuple[Point, ...]:
        """Return a contiguous slice of control points from the node grid.

        Retrieves points from `row` in the half-open interval
        [from_column : to_column]. The method ensures that the slice has the
        expected length and that none of the entries are undefined. This mirrors
        the behavior of the mesh-gradient fallback logic, where missing control
        points indicate a malformed mesh.

        Args:
            row: Row index in the node grid.
            from_column: Starting column index (inclusive).
            to_column: Ending column index (exclusive).

        Returns:
            A tuple of Point objects representing the requested slice.

        Raises:
            IndexError: If the row does not exist, if the slice is incomplete,
                or if any entry inside the slice is undefined.
        """

        try:
            row_list: Sequence[Point | None] = self.nodes[row]
        except IndexError:
            raise IndexError(f"points[{row}] row not defined")

        pts: Sequence[Point | None] = row_list[from_column:to_column]

        if len(pts) != (to_column - from_column):
            raise IndexError(f"points[{row}][{from_column}:{to_column}] not fully defined")

        for idx, p in enumerate(pts):
            if p is None:
                raise IndexError(f"points[{row}][{from_column + idx}] is undefined")

        return cast(tuple[Point, ...], tuple(pts))


    def _find_patch(self, x:float, y:float) -> tuple[int, int] | None:
        """Locate the mesh patch containing a given point.

        Clamps the input coordinates to the mesh's overall bounding box, then
        iterates through all patches to find the one whose geometric bounding box
        contains the clamped point. Each patch is defined by a 4x4 block of control
        points, and its bounds are computed from those points.

        Args:
            x: X-coordinate in mesh-local space (after bbox normalization).
            y: Y-coordinate in mesh-local space.

        Returns:
            A (t, n) tuple identifying the patch row and column if the point lies
            inside a patch's bounding box. Returns None if no patch contains the
            point.
        """

        # Clamp to mesh bounding box
        clamped_x = max(self.xmin, min(x, self.xmax))
        clamped_y = max(self.ymin, min(y, self.ymax))

        self.outer.log(logging.DEBUG, f"({x}, {y}) -> clamped({clamped_x}, {clamped_y})")

        self.outer.log(logging.DEBUG,f"mesh bounds=({self.xmin},{self.ymin})-({self.xmax},{self.ymax})")

        rows = (len(self.nodes) - 1) // 3
        cols = (len(self.nodes[0]) - 1) // 3

        for t in range(rows):
            for n in range(cols):
                # get patch geometry
                patch = [self._points(3*t + i, 3*n,  3*n+4) for i in range(4)]

                # compute bounding box
                xs = [p.x for row in patch for p in row]
                ys = [p.y for row in patch for p in row]

                if min(xs) <= clamped_x <= max(xs) and min(ys) <= clamped_y <= max(ys):
                    self.outer.log(logging.DEBUG,f"[PATCH] ({x:.1f},{y:.1f}) -> ({t},{n})")
                    return (t, n)

        return None


    def _bilinear_uv(self, t: int, n: int, x: float, y: float) -> tuple[float, float]:
        """Compute the (u, v) parameters for a bilinear mesh patch.

        Solves the inverse mapping of a bilinear Coons patch: given a point (x, y)
        in user-space mesh coordinates, this method finds the corresponding
        normalized parameters (u, v) ∈ [0, 1] x [0, 1] inside patch (t, n). The
        solution uses Newton iteration on the bilinear surface defined by the four
        corner points P00, P10, P01, and P11.

        Behavior:
            - Retrieves the four geometric patch corners.
            - Initializes (u, v) to 0.5 for stable convergence.
            - Iteratively solves the bilinear inverse using partial derivatives
            Xu, Yu, Xv, Yv and the surface position (X, Y).
            - Clamps (u, v) to [0, 1] after each iteration to avoid divergence.
            - Stops early if the Jacobian determinant becomes too small.

        Args:
            t: Patch row index.
            n: Patch column index.
            x: X-coordinate inside the mesh's local coordinate system.
            y: Y-coordinate inside the mesh's local coordinate system.

        Returns:
            A tuple (u, v) giving the normalized bilinear coordinates inside the
            patch.
        """

        # corners
        P00 = self._point(3*t,     3*n)
        P10 = self._point(3*t,     3*n+3)
        P01 = self._point(3*t+3,   3*n)
        P11 = self._point(3*t+3,   3*n+3)

        # Newton iteration
        u = v = 0.5
        for _ in range(8):
            # bilinear surface
            Xu = (
                (1-v)*(P10.x - P00.x) +
                v*(P11.x - P01.x)
            )
            Yu = (
                (1-v)*(P10.y - P00.y) +
                v*(P11.y - P01.y)
            )
            Xv = (
                (1-u)*(P01.x - P00.x) +
                u*(P11.x - P10.x)
            )
            Yv = (
                (1-u)*(P01.y - P00.y) +
                u*(P11.y - P10.y)
            )

            X = (
                (1-u)*(1-v)*P00.x +
                u*(1-v)*P10.x +
                (1-u)*v*P01.x +
                u*v*P11.x
            )
            Y = (
                (1-u)*(1-v)*P00.y +
                u*(1-v)*P10.y +
                (1-u)*v*P01.y +
                u*v*P11.y
            )

            dx = X - x
            dy = Y - y

            det = Xu*Yv - Yu*Xv
            if abs(det) < 1e-12:
                break

            du = (Yv*dx - Xv*dy) / det
            dv = (Xu*dy - Yu*dx) / det

            u -= du
            v -= dv

            u = max(0.0, min(1.0, u))
            v = max(0.0, min(1.0, v))

        return (u, v)


    def _bilinear_color(self, t: int, n: int, u: float, v: float):
        """Evaluate the bilinear color at normalized coordinates (u, v).

        Computes the RGBA color inside patch (t, n) using standard bilinear
        interpolation over the four corner colors c00, c10, c01, and c11. Each
        channel is interpolated independently using a two-stage lerp: first along
        the U direction, then along the V direction.

        Args:
            t: Patch row index.
            n: Patch column index.
            u: Horizontal parameter in [0, 1].
            v: Vertical parameter in [0, 1].

        Returns:
            A 4-tuple (r, g, b, a) representing the bilinearly interpolated color
            at (u, v).
        """

        c00 = self._color(t,     n)
        c10 = self._color(t,     n+1)
        c01 = self._color(t+1,   n)
        c11 = self._color(t+1,   n+1)

        def lerp(a, b, w): return a*(1-w) + b*w

        r = lerp(lerp(c00[0], c10[0], u), lerp(c01[0], c11[0], u), v)
        g = lerp(lerp(c00[1], c10[1], u), lerp(c01[1], c11[1], u), v)
        b = lerp(lerp(c00[2], c10[2], u), lerp(c01[2], c11[2], u), v)
        a = lerp(lerp(c00[3], c10[3], u), lerp(c01[3], c11[3], u), v)

        return (r, g, b, a)


    def _bicubic_uv(self, t: int, n: int, x: float, y: float):
        """Invert a bicubic patch to find (u, v) for a given point.

        Given a point (x, y) in mesh-local coordinates, this computes the
        corresponding normalized bicubic parameters (u, v) inside patch (t, n).
        The method performs Newton iteration on the bicubic Coons surface defined
        by the 4x4 control-point grid P.

        Behavior:
            - Extracts the 4x4 geometric control points for the patch.
            - Initializes (u, v) to 0.5 for stable convergence.
            - Uses _bicubic_eval to compute the bicubic surface position (X, Y)
            and its partial derivatives (Xu, Yu, Xv, Yv).
            - Solves the inverse mapping via Newton iteration:
                [du, dv] = J⁻¹ · (X - x, Y - y)
            where J is the Jacobian matrix of partial derivatives.
            - Clamps (u, v) to [0, 1] after each iteration.
            - Stops early if the Jacobian determinant becomes too small.

        Args:
            t: Patch row index.
            n: Patch column index.
            x: X-coordinate inside the mesh's local coordinate system.
            y: Y-coordinate inside the mesh's local coordinate system.

        Returns:
            A tuple (u, v) giving the normalized bicubic coordinates inside the
            patch.
        """

        # extract 4x4 control points
        P = [[self._point(3*t+i, 3*n+j) for j in range(4)] for i in range(4)]

        # initial guess
        u = v = 0.5

        for _ in range(12):
            # compute bicubic surface and derivatives
            X, Y, Xu, Yu, Xv, Yv = self._bicubic_eval(P, u, v)

            dx = X - x
            dy = Y - y

            det = Xu*Yv - Yu*Xv
            if abs(det) < 1e-12:
                break

            du = (Yv*dx - Xv*dy) / det
            dv = (Xu*dy - Yu*dx) / det

            u -= du
            v -= dv

            u = max(0.0, min(1.0, u))
            v = max(0.0, min(1.0, v))

        return (u, v)


    def _bicubic_color(
        self,
        t: int,
        n: int,
        u: float,
        v: float,
    ) -> tuple[float, float, float, float]:
        """Evaluate the bicubic color inside patch (t, n) at parameters (u, v).

        Computes the RGBA color for a bicubic mesh patch using the 4x4 color
        lattice nodes and their directional derivatives. This mirrors the behavior
        of Inkscape's mesh-gradient bicubic evaluator: each channel is reconstructed
        from a 16-element coefficient vector derived from corner colors and scaled
        du/dv derivatives, then evaluated using a bicubic polynomial.

        Behavior:
            - Retrieves the four corner colors c00, c10, c01, c11.
            - Retrieves the corresponding ColorNode entries, including du and dv.
            - Computes geometric distances along U and V to scale derivatives
            consistently with the JS reference implementation.
            - Builds four 16-element coefficient vectors (one per channel) in the
            canonical bicubic order:
                [c00, c10, c01, c11,
                du00, du10, du01, du11,
                dv00, dv10, dv01, dv11,
                0, 0, 0, 0]
            - Applies the bicubic coefficient matrix to each vector.
            - Evaluates the bicubic polynomial at (u, v) for R, G, B, and A.

        Args:
            t: Patch row index.
            n: Patch column index.
            u: Horizontal bicubic parameter in [0, 1].
            v: Vertical bicubic parameter in [0, 1].

        Returns:
            A 4-tuple (r, g, b, a) representing the bicubic interpolated color at
            (u, v).
        """

        c00 = self._color(t, n)
        c10 = self._color(t, n + 1)
        c01 = self._color(t + 1, n)
        c11 = self._color(t + 1, n + 1)

        c00_node, c10_node, c01_node, c11_node = \
            self._lattice_corners(t, n)

        dist_u_left = self._distance(c00_node["point"], c01_node["point"])
        dist_u_right = self._distance(c10_node["point"],c11_node["point"])
        dist_v_top = self._distance(c00_node["point"],c10_node["point"])
        dist_v_bottom = self._distance(c01_node["point"], c11_node["point"])

        d00u = c00_node["du"]
        d10u = c10_node["du"]
        d01u = c01_node["du"]
        d11u = c11_node["du"]

        d00v = c00_node["dv"]
        d10v = c10_node["dv"]
        d01v = c01_node["dv"]
        d11v = c11_node["dv"]

        d_r = [
            c00[0], c10[0], c01[0], c11[0],

            d00u[0] * dist_u_left,
            d10u[0] * dist_u_left,
            d01u[0] * dist_u_right,
            d11u[0] * dist_u_right,

            d00v[0] * dist_v_top,
            d10v[0] * dist_v_bottom,
            d01v[0] * dist_v_top,
            d11v[0] * dist_v_bottom,

            0.0, 0.0, 0.0, 0.0,
        ]

        d_g = [
            c00[1], c10[1], c01[1], c11[1],

            d00u[1] * dist_u_left,
            d10u[1] * dist_u_left,
            d01u[1] * dist_u_right,
            d11u[1] * dist_u_right,

            d00v[1] * dist_v_top,
            d10v[1] * dist_v_bottom,
            d01v[1] * dist_v_top,
            d11v[1] * dist_v_bottom,

            0.0, 0.0, 0.0, 0.0,
        ]

        d_b = [
            c00[2], c10[2], c01[2], c11[2],

            d00u[2] * dist_u_left,
            d10u[2] * dist_u_left,
            d01u[2] * dist_u_right,
            d11u[2] * dist_u_right,

            d00v[2] * dist_v_top,
            d10v[2] * dist_v_bottom,
            d01v[2] * dist_v_top,
            d11v[2] * dist_v_bottom,

            0.0, 0.0, 0.0, 0.0,
        ]

        d_a = [
            c00[3], c10[3], c01[3], c11[3],

            d00u[3] * dist_u_left,
            d10u[3] * dist_u_left,
            d01u[3] * dist_u_right,
            d11u[3] * dist_u_right,

            d00v[3] * dist_v_top,
            d10v[3] * dist_v_bottom,
            d01v[3] * dist_v_top,
            d11v[3] * dist_v_bottom,

            0.0, 0.0, 0.0, 0.0,
        ]

        coeff_r = self._applyCoefficientMatrix (d_r)
        coeff_g = self._applyCoefficientMatrix (d_g)
        coeff_b = self._applyCoefficientMatrix (d_b)
        coeff_a = self._applyCoefficientMatrix (d_a)

        r = self._evaluateBicubicPolynomial (coeff_r, u, v)
        g = self._evaluateBicubicPolynomial (coeff_g, u, v)
        b = self._evaluateBicubicPolynomial (coeff_b, u, v)
        a = self._evaluateBicubicPolynomial (coeff_a, u, v)

        return (r, g, b, a)


    def _applyCoefficientMatrix (self, t: list[float]) -> list[float]:
        """Apply the bicubic coefficient matrix to a 16-element vector.

        Multiplies the input vector `t` by the 16x16 U_MATRIX used in bicubic
        mesh-gradient reconstruction. Each output entry is the dot product of one
        row of U_MATRIX with the input vector. This produces the 16 bicubic
        polynomial coefficients for a single color channel.

        Args:
            t: A 16-element list of floats containing corner colors, scaled
            directional derivatives, and placeholder zeros.

        Returns:
            A 16-element list of floats representing the bicubic polynomial
            coefficients for the channel.
        """

        out = [0.0] * 16
        for i in range(16):
            s = 0.0
            row = self.U_MATRIX[i]
            for j in range(16):
                s += row[j] * t[j]
            out[i] = s
        return out


    def _evaluateBicubicPolynomial(self, c: list[float], u: float, v: float) -> float:
        """Evaluate a bicubic polynomial at parameters (u, v).

        Computes the value of a bicubic polynomial using its 16 coefficients `c`
        arranged in the canonical tensor-product basis:

            Σᵢ₌₀³ Σⱼ₌₀³  c[4*i + j] · uⁱ · vʲ

        The implementation expands the polynomial explicitly for performance and
        to match the structure used in mesh-gradient evaluators.

        Args:
            c: A 16-element coefficient list produced by apply coefficient matrix().
            u: Horizontal parameter in [0, 1].
            v: Vertical parameter in [0, 1].

        Returns:
            The scalar bicubic value at (u, v).
        """

        uu = u*u
        uuu = uu*u
        vv = v*v
        vvv = vv*v

        return (
            c[0] +
            c[1]*u + c[2]*uu + c[3]*uuu +
            c[4]*v + c[5]*v*u + c[6]*v*uu + c[7]*v*uuu +
            c[8]*vv + c[9]*vv*u + c[10]*vv*uu + c[11]*vv*uuu +
            c[12]*vvv + c[13]*vvv*u + c[14]*vvv*uu + c[15]*vvv*uuu
        )


    def _bicubic_eval(self, P: list[list[Point]], u: float, v: float):
        """Evaluate a bicubic surface and its partial derivatives.

        Computes the bicubic geometry value (X, Y) and its first-order partial
        derivatives (Xu, Yu, Xv, Yv) at parameters (u, v). The method mirrors the
        structure of bicubic mesh-gradient geometry evaluation:

            1. Flatten the 4x4 control-point grid P into two 16-element vectors
            containing x- and y-coordinates.
            2. Apply the bicubic coefficient matrix to obtain polynomial
            coefficients for x and y.
            3. Evaluate the bicubic polynomials at (u, v) to obtain X and Y.
            4. Compute ∂/∂u and ∂/∂v by applying u-derivative coefficients
            and v-derivative coefficients, then evaluating the resulting polynomials.

        Args:
            P: A 4x4 list of Point objects defining the bicubic patch geometry.
            u: Horizontal parameter in [0, 1].
            v: Vertical parameter in [0, 1].

        Returns:
            A 6-tuple (X, Y, Xu, Yu, Xv, Yv) containing the bicubic surface
            position and its partial derivatives at (u, v).
        """

        # Build 16 geometry vectors for x and y
        tx = []
        ty = []

        for i in range(4):
            for j in range(4):
                tx.append(P[i][j].x)
                ty.append(P[i][j].y)

        # Compute coefficients
        cx = self._applyCoefficientMatrix (tx)
        cy = self._applyCoefficientMatrix (ty)

        # Evaluate surface
        X = self._evaluateBicubicPolynomial (cx, u, v)
        Y = self._evaluateBicubicPolynomial (cy, u, v)

        # Derivatives: ∂/∂u and ∂/∂v
        Xu = self._evaluateBicubicPolynomial (self._derivative_u(cx), u, v)
        Yu = self._evaluateBicubicPolynomial (self._derivative_u(cy), u, v)

        Xv = self._evaluateBicubicPolynomial (self._derivative_v(cx), u, v)
        Yv = self._evaluateBicubicPolynomial (self._derivative_v(cy), u, v)

        return X, Y, Xu, Yu, Xv, Yv

    def _derivative_u(self, c: list[float]) -> list[float]:
        """Return the ∂/∂u coefficients of a bicubic polynomial.

        Given a 16-element bicubic coefficient vector `c`, this computes the
        coefficient vector for the partial derivative with respect to `u`. The
        bicubic basis is arranged in tensor-product form:

            c[4*i + j] · uⁱ · vʲ    for i, j ∈ {0, 1, 2, 3}

        Differentiating with respect to `u` yields:

            ∂/∂u (u⁰) = 0
            ∂/∂u (u¹) = 1
            ∂/∂u (u²) = 2u
            ∂/∂u (u³) = 3u²

        The method applies these factors to each v-layer, producing a new
        16-element coefficient list suitable for evaluation with bicubic polynomial.

        Args:
            c: A 16-element bicubic coefficient list.

        Returns:
            A 16-element list representing the ∂/∂u coefficient vector.
        """

        out = [0.0]*16
        # derivative of u^0 is 0
        out[1] = c[1]
        out[2] = 2*c[2]
        out[3] = 3*c[3]

        # v terms
        out[5] = c[5]
        out[6] = 2*c[6]
        out[7] = 3*c[7]

        # v^2 terms
        out[9]  = c[9]
        out[10] = 2*c[10]
        out[11] = 3*c[11]

        # v^3 terms
        out[13] = c[13]
        out[14] = 2*c[14]
        out[15] = 3*c[15]

        return out


    def _derivative_v(self, c: list[float]) -> list[float]:
        """Return the ∂/∂v coefficients of a bicubic polynomial.

        Given a 16-element bicubic coefficient vector `c`, this computes the
        coefficient vector for the partial derivative with respect to `v`. The
        bicubic basis is arranged in tensor-product form:

            c[4*i + j] · uⁱ · vʲ    for i, j ∈ {0, 1, 2, 3}

        Differentiating with respect to `v` yields:

            ∂/∂v (v⁰) = 0
            ∂/∂v (v¹) = 1
            ∂/∂v (v²) = 2v
            ∂/∂v (v³) = 3v²

        The method applies these factors across all u-layers, producing a new
        16-element coefficient list suitable for evaluation with bicubic polynomial.

        Args:
            c: A 16-element bicubic coefficient list.

        Returns:
            A 16-element list representing the ∂/∂v coefficient vector.
        """

        out = [0.0]*16

        # v terms
        out[4] = c[4]
        out[5] = c[5]
        out[6] = c[6]
        out[7] = c[7]

        # v^2 terms
        out[8]  = 2*c[8]
        out[9]  = 2*c[9]
        out[10] = 2*c[10]
        out[11] = 2*c[11]

        # v^3 terms
        out[12] = 3*c[12]
        out[13] = 3*c[13]
        out[14] = 3*c[14]
        out[15] = 3*c[15]

        return out


    def _ensure_node(self, r: int, c: int):
        """Ensure that a node exists at lattice position (r, c).

        Expands the 2D node grid `self.nodes` as needed so that the entry
        `self.nodes[r][c]` is guaranteed to exist. Missing rows are appended as
        empty lists, and missing columns inside an existing row are filled with
        Point(0, 0). This method provides safe allocation for later geometry
        construction routines that assume a fully populated lattice.

        Args:
            r: Row index to ensure.
            c: Column index to ensure.

        Returns:
            None. The node grid is modified in place.

        Raises:
            None. The method always allocates missing entries.
        """

        while len(self.nodes) <= r:
            self.nodes.append([])
        while len(self.nodes[r]) <= c:
            self.nodes[r].append(Point(0,0))


    def _ensure_color(self, r: int, c: int):
        """Ensure that a color entry exists at lattice position (r, c).

        Expands the 2D color grid `self.colors` so that `self.colors[r][c]` is
        guaranteed to exist. Missing rows are appended as empty lists, and missing
        columns inside an existing row are filled with `None`, matching the mesh
        initializer's convention for “color not yet assigned.”

        This provides safe allocation for later routines that populate or validate
        the color lattice, such as
            - point sampling
            - color lookup

        Args:
            r: Row index to ensure.
            c: Column index to ensure.

        Returns:
            None. The color grid is modified in place.
        """

        while len(self.colors) <= r:
            self.colors.append([])
        while len(self.colors[r]) <= c:
            self.colors[r].append(None)

    
    def _apply_edge(self, t: int, n: int, i: int, cmd: str, coords: List[Point]):
        """Apply an SVG path edge command to patch (t, n).

        Interprets a single edge-construction command ('l'/'L' for line,
        'c'/'C' for cubic Bézier) and writes the corresponding control points into
        the 4x4 bicubic lattice for patch (t, n). Each patch edge consists of four
        nodes, located at indices:

            TOP:    (T,   N .. N+3)
            RIGHT:  (T..T+3, N+3)
            BOTTOM: (T+3, N .. N+3)
            LEFT:   (T..T+3, N)

        where T = 3*t and N = 3*n.

        Behavior:
            - Determines whether the command is relative ('l', 'c') or absolute
            ('L', 'C').
            - Selects the edge based on i:
                0 → top edge
                1 → right edge
                2 → bottom edge
                3 → left edge
            - Ensures all required lattice nodes exist via _ensure_node.
            - For line commands:
                - Computes the end point.
                - Fills intermediate control points using 1/3 and 2/3 linear
                interpolation between start and end.
            - For cubic commands:
                - Writes the three provided control points directly (absolute or
                relative to the edge's base point).
            - Handles the special case on the bottom edge when n == 0, where the
            leftmost bottom node must be explicitly set.

        Args:
            t: Patch row index.
            n: Patch column index.
            i: Edge index (0 = top, 1 = right, 2 = bottom, 3 = left).
            cmd: SVG path command: 'l', 'L', 'c', or 'C'.
            coords: List of Point objects representing command parameters.

        Returns:
            None. The node lattice is modified in place.
        """

        rel = cmd in ("l", "c")

        def P(pt: Point, base: Point) -> Point:
            return pt.add(base) if rel else pt

        T = 3 * t
        N = 3 * n

        #
        # TOP EDGE
        #
        if i == 0:

            if cmd in ("l", "L"):
                end = P(coords[0], self._point(T, N))

                self._ensure_node(T, N + 1)
                self._ensure_node(T, N + 2)
                self._ensure_node(T, N + 3)

                self.nodes[T][N + 3] = end
                self.nodes[T][N + 1] = Point(
                    (2 * self._point(T, N).x + end.x) / 3,
                    (2 * self._point(T, N).y + end.y) / 3,
                )
                self.nodes[T][N + 2] = Point(
                    (2 * end.x + self._point(T, N).x) / 3,
                    (2 * end.y + self._point(T, N).y) / 3,
                )

            elif cmd in ("c", "C"):
                self._ensure_node(T, N + 1)
                self._ensure_node(T, N + 2)
                self._ensure_node(T, N + 3)

                base = self._point(T, N)

                self.nodes[T][N + 1] = P(coords[0], base)
                self.nodes[T][N + 2] = P(coords[1], base)
                self.nodes[T][N + 3] = P(coords[2], base)

        #
        # RIGHT EDGE
        #
        elif i == 1:

            if cmd in ("l", "L"):
                end = P(coords[0], self._point(T, N + 3))

                self._ensure_node(T + 1, N + 3)
                self._ensure_node(T + 2, N + 3)
                self._ensure_node(T + 3, N + 3)

                self.nodes[T + 3][N + 3] = end
                self.nodes[T + 1][N + 3] = Point(
                    (2 * self._point(T, N + 3).x + end.x) / 3,
                    (2 * self._point(T, N + 3).y + end.y) / 3,
                )
                self.nodes[T + 2][N + 3] = Point(
                    (2 * end.x + self._point(T, N + 3).x) / 3,
                    (2 * end.y + self._point(T, N + 3).y) / 3,
                )

            elif cmd in ("c", "C"):
                self._ensure_node(T + 1, N + 3)
                self._ensure_node(T + 2, N + 3)
                self._ensure_node(T + 3, N + 3)

                base = self._point(T, N + 3)

                self.nodes[T + 1][N + 3] = P(coords[0], base)
                self.nodes[T + 2][N + 3] = P(coords[1], base)
                self.nodes[T + 3][N + 3] = P(coords[2], base)

        #
        # BOTTOM EDGE
        #
        elif i == 2:

            if cmd in ("l", "L"):

                if n == 0:
                    self.nodes[T + 3][N] = P(coords[0], self._point(T + 3, N + 3))

                self._ensure_node(T + 3, N + 1)
                self._ensure_node(T + 3, N + 2)

                self.nodes[T + 3][N + 1] = Point(
                    (2 * self._point(T + 3, N).x +
                    self._point(T + 3, N + 3).x) / 3,
                    (2 * self._point(T + 3, N).y +
                    self._point(T + 3, N + 3).y) / 3,
                )

                self.nodes[T + 3][N + 2] = Point(
                    (2 * self._point(T + 3, N + 3).x +
                    self._point(T + 3, N).x) / 3,
                    (2 * self._point(T + 3, N + 3).y +
                    self._point(T + 3, N).y) / 3,
                )

            elif cmd in ("c", "C"):

                self._ensure_node(T + 3, N + 2)
                self._ensure_node(T + 3, N + 1)

                base = self._point(T + 3, N + 3)

                self.nodes[T + 3][N + 2] = P(coords[0], base)
                self.nodes[T + 3][N + 1] = P(coords[1], base)

                if n == 0:
                    self._ensure_node(T + 3, N)
                    self.nodes[T + 3][N] = P(coords[2], base)

        #
        # LEFT EDGE
        #
        else:

            if cmd in ("l", "L"):

                self._ensure_node(T + 1, N)
                self._ensure_node(T + 2, N)

                self.nodes[T + 1][N] = Point(
                    (2 * self._point(T, N).x +
                    self._point(T + 3, N).x) / 3,
                    (2 * self._point(T, N).y +
                    self._point(T + 3, N).y) / 3,
                )

                self.nodes[T + 2][N] = Point(
                    (2 * self._point(T + 3, N).x +
                    self._point(T, N).x) / 3,
                    (2 * self._point(T + 3, N).y +
                    self._point(T, N).y) / 3,
                )

            elif cmd in ("c", "C"):

                base = self._point(T + 3, N)

                self._ensure_node(T + 2, N)
                self._ensure_node(T + 1, N)

                self.nodes[T + 2][N] = P(coords[0], base)
                self.nodes[T + 1][N] = P(coords[1], base)


    def _apply_color(self, t: int, n: int, i: int, stop_el: Stop):
        """Apply a mesh-gradient color stop to patch (t, n).

        Extracts RGBA values from an SVG <stop> element and assigns them to the
        appropriate corner of the color lattice for patch (t, n). Each patch has
        four color entries arranged as:

            i = 0 → top-left     (t,     n)
            i = 1 → top-right    (t,     n+1)
            i = 2 → bottom-right (t+1,   n+1)
            i = 3 → bottom-left  (t+1,   n)

        Behavior:
            - Parses the stop element using _extract_rgba.
            - Ensures the four patch-corner color slots exist via _ensure_color.
            - Writes the RGBA tuple into the correct lattice position based on i.
            - Logs the assignment for debugging.

        Args:
            t: Patch row index.
            n: Patch column index.
            i: Corner index (0 = TL, 1 = TR, 2 = BR, 3 = BL).
            stop_el: The SVG <stop> element providing color and opacity.

        Returns:
            None. The color lattice is modified in place.
        """

        rgba = self._extract_rgba(stop_el)

        if rgba is None:
            return

        self._ensure_color(t, n)
        self._ensure_color(t, n + 1)
        self._ensure_color(t + 1, n)
        self._ensure_color(t + 1, n + 1)

        if i == 0:
            self.colors[t][n] = rgba

        elif i == 1:
            self.colors[t][n + 1] = rgba

        elif i == 2:
            self.colors[t + 1][n + 1] = rgba

        elif i == 3:
            self.colors[t + 1][n] = rgba

        self.outer.log(
            logging.DEBUG,
            f"[COLOR] t={t} n={n} i={i} rgba={rgba}"
        )


    def _compute_interior(self, t: int, n: int):
        """Compute the four interior control points of patch (t, n).

        Fills the bicubic mesh lattice's interior geometry for the patch defined by
        its 4x4 control-point block. The method applies the standard 9-point
        interpolation formula used in mesh-gradient implementations to derive the
        interior points from the surrounding edge and corner nodes.

        Only the four interior nodes are computed:

            (T+1, N+1), (T+1, N+2),
            (T+2, N+1), (T+2, N+2)

        where T = 3*t and N = 3*n.

        Behavior:
            - Ensures the four interior nodes exist via _ensure_node.
            - Retrieves all required surrounding nodes using _point.
            - Applies the 9-point stencil to compute X and Y coordinates for each
            interior node. The formula blends:
                * the four corners,
                * the four edge midpoints,
                * the opposite edge endpoints,
            using fixed weights and divides the result by 9.
            - Writes the computed coordinates directly into the lattice.

        Args:
            t: Patch row index.
            n: Patch column index.

        Returns:
            None. The geometry lattice is modified in place.
        """

        # allocate interior points (but do NOT create defaults)
        for dr in (1, 2):
            for dc in (1, 2):
                r = 3 * t + dr
                c = 3 * n + dc
                self._ensure_node(r, c)

        T = 3 * t
        N = 3 * n

        # convenience alias
        p = self._point

        # ---- X coordinates ----
        self._point(T+1, N+1).x = (
            -4 * p(T,   N).x
            + 6 * (p(T,   N+1).x + p(T+1, N).x)
            - 2 * (p(T,   N+3).x + p(T+3, N).x)
            + 3 * (p(T+3, N+1).x + p(T+1, N+3).x)
            -     p(T+3, N+3).x
        ) / 9.0

        self._point(T+1, N+2).x = (
            -4 * p(T,   N+3).x
            + 6 * (p(T,   N+2).x + p(T+1, N+3).x)
            - 2 * (p(T,   N).x   + p(T+3, N+3).x)
            + 3 * (p(T+3, N+2).x + p(T+1, N).x)
            -     p(T+3, N).x
        ) / 9.0

        self._point(T+2, N+1).x = (
            -4 * p(T+3, N).x
            + 6 * (p(T+3, N+1).x + p(T+2, N).x)
            - 2 * (p(T+3, N+3).x + p(T,   N).x)
            + 3 * (p(T,   N+1).x + p(T+2, N+3).x)
            -     p(T,   N+3).x
        ) / 9.0

        self._point(T+2, N+2).x = (
            -4 * p(T+3, N+3).x
            + 6 * (p(T+3, N+2).x + p(T+2, N+3).x)
            - 2 * (p(T+3, N).x   + p(T,   N+3).x)
            + 3 * (p(T,   N+2).x + p(T+2, N).x)
            -     p(T,   N).x
        ) / 9.0

        # ---- Y coordinates ----
        self._point(T+1, N+1).y = (
            -4 * p(T,   N).y
            + 6 * (p(T,   N+1).y + p(T+1, N).y)
            - 2 * (p(T,   N+3).y + p(T+3, N).y)
            + 3 * (p(T+3, N+1).y + p(T+1, N+3).y)
            -     p(T+3, N+3).y
        ) / 9.0

        self._point(T+1, N+2).y = (
            -4 * p(T,   N+3).y
            + 6 * (p(T,   N+2).y + p(T+1, N+3).y)
            - 2 * (p(T,   N).y   + p(T+3, N+3).y)
            + 3 * (p(T+3, N+2).y + p(T+1, N).y)
            -     p(T+3, N).y
        ) / 9.0

        self._point(T+2, N+1).y = (
            -4 * p(T+3, N).y
            + 6 * (p(T+3, N+1).y + p(T+2, N).y)
            - 2 * (p(T+3, N+3).y + p(T,   N).y)
            + 3 * (p(T,   N+1).y + p(T+2, N+3).y)
            -     p(T,   N+3).y
        ) / 9.0

        self._point(T+2, N+2).y = (
            -4 * p(T+3, N+3).y
            + 6 * (p(T+3, N+2).y + p(T+2, N+3).y)
            - 2 * (p(T+3, N).y   + p(T,   N+3).y)
            + 3 * (p(T,   N+2).y + p(T+2, N).y)
            -     p(T,   N).y
        ) / 9.0


    def _midpoint(self, a: Point, b: Point) -> Point:
        """Return the midpoint between two points.

        Computes the point halfway between a and b by averaging their x and y
        coordinates. Used throughout the mesh construction pipeline whenever
        uniform subdivision of an edge is required.

        Args:
            a: First point.
            b: Second point.

        Returns:
            A new Point located at the midpoint of a and b.
        """

        return Point(0.5 * (a.x + b.x), 0.5 * (a.y + b.y))

    
    def _parse_coords(self, s: str) -> List[Point]:
        """Parse a coordinate string into a list of Points.

        Splits an SVG coordinate string by spaces or commas and converts each
        numeric pair into a Point. The function assumes the string contains an
        even number of numeric values representing (x, y) pairs.

        Args:
            s: A string containing coordinate values, e.g. "10,20 30,40".

        Returns:
            A list of Point objects parsed from the string.
        """

        # split by space/comma, pair into points
        parts = re.split(r"[ ,]+", s.strip())
        coords: List[Point] = []
        for i in range(0, len(parts) - 1, 2):
            coords.append(Point(float(parts[i]), float(parts[i + 1])))
        return coords


    def _extract_rgba(self, stop_el) -> Tuple[float, float, float, float] | None:
        """Extract an RGBA tuple from an SVG <stop> element.

        Parses the 'stop-color' and 'stop-opacity' attributes of a mesh-gradient
        stop element and converts them into a normalized RGBA tuple. Color parsing
        is delegated to the Color(...) class, which supports named colors, hex
        codes, rgb()/rgba(), hsl()/hsla(), and other SVG color syntaxes.

        Behavior:
            - If 'stop-color' is missing, returns None so that fallback logic can
            assign a default color.
            - Converts the parsed color into floating r, g, b components.
            - Parses 'stop-opacity' if present; otherwise defaults to 1.0.
            - Combines the color's intrinsic alpha with stop-opacity to produce
            the final alpha value.

        Args:
            stop_el: The SVG <stop> element providing color and opacity.

        Returns:
            A tuple (r, g, b, a) with float components, or None if no color is
            specified.
        """

        color_str = stop_el.get("stop-color")
        opacity_str = stop_el.get("stop-opacity")

        # Missing color → let fallback handle it
        if color_str is None:
            return None

        col = Color(color_str)

        r = float(col.red)
        g = float(col.green)
        b = float(col.blue)

        try:
            stop_opacity = float(opacity_str) if opacity_str is not None else 1.0
        except ValueError:
            stop_opacity = 1.0

        base_alpha = float(col.alpha) * 255.0
        a = base_alpha * stop_opacity

        return (r, g, b, a)

 
    # endregion

    