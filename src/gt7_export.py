#!/usr/bin/env python3
from ast import Not
from platform import node
from pydoc import doc
import traceback
import re
import copy
from turtle import shape
from unittest import result
import inkex
import inkex.command
from inkex.styles import Style
from inkex.transforms import Transform
import sys
import inspect
import os
import tempfile
import logging
import io
from datetime import datetime
import numpy as np
from numpy import clip
from PIL import Image
import math

LOG_LEVEL = logging.DEBUG

# region log_args

def log_args(func):
    func._log_args = True
    return func

# endregion

# region NullWriter
class NullWriter:
    def write(self, *args, **kwargs):
        pass
    def flush(self):
        pass

# endregion

class GT7Export(inkex.OutputExtension):
    """Save As → GT7 SVG"""
    
    # region constants

    STRIP_ALPHA_FROM_GRADIENTS = True
    COORDINATE_ROUNDING_PRECISION = 3
    GRADIENT_MESH_PATCH_DIVISIONS = 2
    COMPRESS_OUTPUT = False

    GT7_ATTRS = {
        "id",
        "d",
        "x", "y",
        "cx", "cy", 
        "r", "rx", "ry",
        "width", "height",
        "transform",
        "fill", "stroke", "stroke-width",
        "fill-rule",
        "stroke-linecap", "stroke-linejoin", "stroke-miterlimit",
        "opacity", "fill-opacity", "stroke-opacity",
        "clip-path",
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
        "stop-color", "stop-opacity",
    }


    PAINT_SERVER_TAGS = {
        "linearGradient",
        "radialGradient",
        "pattern",
        "marker",
        "filter",
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
    
    DEFAULTS = {
        "fill-rule": "nonzero",
        "stroke": "none",
        "stroke-width": "1",
        "stroke-linecap": "butt",
        "stroke-linejoin": "miter",
        "stroke-miterlimit": "4",
        "opacity": "1",
        "fill-opacity": "1",
        "stroke-opacity": "1",
    }

    REFERENCEABLE_TAGS = {
        "clipPath", "mask", "filter",
        "pattern", "linearGradient", "radialGradient",
        "symbol", "marker"
    }


    XLINK_NS = "http://www.w3.org/1999/xlink"

    # endregion

    # region --- inkex.OutputExtension interface ---

    @log_args
    def __init__(self):
        super().__init__()

        self.REF_ATTRS = (
            "clip-path", "mask", "filter",
            "fill", "stroke",
            "marker-start", "marker-mid", "marker-end",
            "href", f"{{{self.XLINK_NS}}}href"
        )

        self.id_counters = {}

        # ---------------------------------------------------------
        # Logging setup
        # ---------------------------------------------------------
        self.logger = logging.getLogger("gt7_exporter")
        self.logger.setLevel(LOG_LEVEL)
        
        null_handler = logging.StreamHandler(stream=NullWriter())
        formatter = logging.Formatter(
            "[%(levelname)s] %(funcName)s:%(lineno)d: %(message)s"
        )
        null_handler.setFormatter(formatter)
        self.logger.addHandler(null_handler)

        # File handler will be added later in create_changelog()
        self.file_handler = None
        # ---------------------------------------------------------

    def create_changelog(self, stream):
        outname = getattr(stream, "name", None)

        if not outname or outname.startswith("<"):
            self.log_path = os.path.join(tempfile.gettempdir(), "gt7_export.log")
        else:
            self.log_path = outname + ".log"

        if not self.file_handler:
            file_handler = logging.FileHandler(self.log_path, mode="w", encoding="utf-8")
            formatter = logging.Formatter(
                "[%(levelname)s] %(funcName)s(%(lineno)d): %(message)s"
            )
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)
            self.file_handler = file_handler

        self.log(logging.INFO, f"Writing GT7 log to {self.log_path}")

    def add_arguments(self, pars):
        pass
        
    def effect(self):
        pass

    def save(self, stream):
        try:
            
            self.create_changelog(stream)
        
            self.log(logging.INFO, f"Started @ {datetime.now().isoformat()}")
            self.log(logging.INFO, f"Python executable: {sys.executable}")
            self.log(logging.INFO, f"inkex loaded from: {inspect.getfile(inkex)}")
            self.log(logging.INFO, f"inkex version: {getattr(inkex, '__version__', 'NO VERSION ATTRIBUTE')}")

            self.preprocess(types_to_path=["text"], unlink_clones=True)
            
            self.resolve_styles_to_attributes()
            self.log_svg(header="BEFORE expand_all_uses()")
            self.expand_all_uses()
            self.log_svg(header="AFTER expand_all_uses()")
            self.resolve_references()
            self.log_svg(header="AFTER resolve_references()")
            self.replace_unsupported_shapes()
            self.log_svg(header="AFTER replace_unsupported_shapes()")
            
            self.remove_all_groups()
            self.log_svg(header="AFTER remove_all_groups()")

            transform_count = self.apply_all_transforms()
            self.log(logging.INFO, f"Resolved {transform_count} transformations into plain geometry")
            self.log_svg(header="AFTER apply_all_transforms()")

            clip_count = self.remove_all_clippaths()
            self.log(logging.INFO, f"Resolved {clip_count} clip-paths into plain geometry")
            self.log_svg(header="AFTER remove_all_clippaths()")
            
            self.translate_viewbox()
            self.clean_stroke_attributes()
            self.compress_output()
            self.cleanup_defs()
            self.remove_unreferenced_referenceables()
            self.log_svg(header="AFTER cleanup_defs()")

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

    def log(self, level, msg, stacklevel=2):
        """
        Unified logging wrapper:
        - Python logging (with real caller info)
        - self.msg mirror
        """

        if level == logging.ERROR:
            self.logger.error(msg, stacklevel=stacklevel)
            self.msg(f"[ERROR] {msg}")
            
        elif level == logging.WARNING:
            self.logger.error(msg, stacklevel=stacklevel)
            self.msg(f"[WARNING] {msg}")

        elif level == logging.INFO:
            if self.logger.isEnabledFor(logging.INFO):
                self.logger.info(msg, stacklevel=stacklevel)

        elif level == logging.DEBUG:
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(msg, stacklevel=stacklevel)


    def log_svg(self, node=None, indent=0, header="SVG DOM"):
        """
        Recursively log the entire SVG DOM tree with indentation.
        Shows:
        - tag name
        - id (if present)
        - attributes
        - children
        """

        if header:
            self.log(logging.DEBUG, f"-------------------- {header} --------------------")

        if node is None:
            node = self.svg

        tag = self.tag_name(node)
        attrs = " ".join(f"{k}='{v}'" for k, v in node.attrib.items())
        pad = "  " * indent

        self.log(logging.DEBUG, f"{pad}<{tag} {attrs}>", stacklevel=3)

        for child in node:
            self.log_svg(child, indent + 1, "")

        if header:
            self.log(logging.DEBUG, f"-----------------------------------------------")


    # endregion

    # region --- DOM Tree and SVG Helpers ---

    def generate_id(self, el):
        """
        Generate a short ID: first letter of tag name (uppercase) + counter.
        Ensures uniqueness via find_node().
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

    def url_to_id(self, url):
        if not url:
            return None

        url = url.strip()

        # Case 1: url(#foo)
        if url.startswith("url(") and url.endswith(")"):
            inside = url[4:-1].strip()   # → "#foo"
            if inside.startswith("#"):
                return inside[1:]        # → "foo"
            return inside                # fallback

        # Case 2: #foo
        if url.startswith("#"):
            return url[1:]

        # Case 3: plain "foo" (not valid for markers, but safe fallback)
        return url

    
    def ref_target(self, el, attr="href"):
        href = el.get(attr) or el.get(f"{{{self.XLINK_NS}}}{attr}")
        id = self.url_to_id(href)
        if id is None:
            return None, None
        return self.find_node(id), id

    def parent_of(self, child):
        parent = child.getparent()
        if parent is None:
            return None, 0
        return parent, parent.index(child)
    
    def find_node(self, id):
        node = self.svg.getElementById(id)

        if node is None:
            result = self.svg.xpath(f"//*[@id='{id}']")
            node = result[0] if result else None

        if node is None:
            self.log(logging.DEBUG, f"Node with id='{id}' not found")

        return node

    def node_str(self, node):
        return f"<{self.tag_name(node)}, id={node.get("id")}, attrib={dict(node.attrib)}>"

    def add_node(self, node, parent, index=None):
        tag = node.tag.split('}')[-1]

        # ---------------------------------------------------------
        # Strip STOP IDs immediately
        # ---------------------------------------------------------
        if tag in { "stop", "defs" }:
            node.attrib.pop("id", None)
            self.log(logging.DEBUG, f"[ADD_NODE]   {tag} element → stripping ID")
        else:
            # ---------------------------------------------------------
            # Assign ID to gradients, paths, groups, etc.
            # ---------------------------------------------------------
            if not node.get("id"):
                new_id = self.generate_id(node)
                node.set("id", new_id)
                self.log(logging.DEBUG, f"[ADD_NODE]   assigned id={new_id} to <{tag}>")
            else:
                self.log(logging.DEBUG, f"[ADD_NODE]   existing id={node.get('id')} on <{tag}>")

        # ---------------------------------------------------------
        # Insert node
        # ---------------------------------------------------------
        if index is None:
            parent.append(node)
            self.log(logging.DEBUG, f"[ADD_NODE]   appended <{tag}>")
        else:
            parent.insert(index, node)
            self.log(logging.DEBUG, f"[ADD_NODE]   inserted <{tag}> at index={index}")

        # ---------------------------------------------------------
        # Assign IDs to descendants (except stops)
        # ---------------------------------------------------------
        for el in node.iter():
            etag = el.tag.split('}')[-1]
            if etag == "stop":
                el.attrib.pop("id", None)
                self.log(logging.DEBUG, f"[ADD_NODE]   descendant <stop> → stripping ID")
                continue

            if not el.get("id"):
                new_id = self.generate_id(el)
                el.set("id", new_id)
                self.log(logging.DEBUG, f"[ADD_NODE]   descendant <{etag}> assigned id={new_id}")

        return node


    def remove_node(self, node, parent=None):
        if parent is None:
            parent = node.getparent()
        if parent is not None:
            parent.remove(node)
            
    def tag_name(self, el):
        tag = el.tag
        if not isinstance(tag, str):
            return ""
        return tag.split("}")[-1]

    def is_expandable_ref(self, ref_el):
        return self.tag_name(ref_el) not in self.SKIP_RESOLVE_TAGS

    def copy_presentation_attributes(self, src, dst, override=True):
        for attr in self.PRESENTATION_ATTRS:
            if attr in src.attrib and (override or attr not in dst.attrib):
                dst.set(attr, src.get(attr))

                
    def parse_number(self, value):
        if value is None:
            return 0.0
        s = str(value).strip()
        if not s:
            return 0.0
        try:
            return float(s)
        except ValueError as e:
            self.log(logging.WARNING, f"Cannot convert {value} to float")
            self.log(logging.WARNING, str(e))
            self.log(logging.WARNING, traceback.format_exc())
            return 0.0
            
    def round_floats_in_string(self, s, digits=3):
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
            return rounded

        return re.sub(float_re, repl, s, flags=re.VERBOSE)

            
    def is_geometry(self, el, gt7_supported=True):
        """
        Identify geometry nodes.
        full=False → GT7-safe geometry only
        full=True  → all SVG geometry types that can be converted to paths
        """

        tag = self.tag_name(el)

        if gt7_supported:
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
        
    def use_transform(self, use_el):
        tx = self.parse_number(use_el.get("x"))
        ty = self.parse_number(use_el.get("y"))
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

    def append_transform(self, el, t):
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


    def transform_path(self, node, transform):
        # Parse existing path, preserving all subpaths
        p = node.path.to_absolute()

        # Apply CTM safely
        p = p.transform(transform)

        # Write back
        node.path = p

        return node

            
    def transform_circle(self, node, transform):
        cx = float(node.get("cx", "0"))
        cy = float(node.get("cy", "0"))
        r  = float(node.get("r", "0"))

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
            node.set("r",  str(r * abs(a)))
            return node

        # Pure rotation around origin
        if a == d and b == -c:
            cx2 = a * cx + c * cy + e
            cy2 = b * cx + d * cy + f
            node.set("cx", str(cx2))
            node.set("cy", str(cy2))
            return node

        # Anything else → circle becomes ellipse → convert to path
        return self.circle_to_path(node, transform=transform)
    
    def transform_rect(self, node, transform):
        x = float(node.get("x", "0"))
        y = float(node.get("y", "0"))
        w = float(node.get("width", "0"))
        h = float(node.get("height", "0"))

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

    def transform_ellipse(self, node, transform):
        cx = float(node.get("cx", "0"))
        cy = float(node.get("cy", "0"))
        rx = float(node.get("rx", "0"))
        ry = float(node.get("ry", "0"))

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

    # endregion

    # region --- Resolve Styles ---

    def resolve_styles_to_attributes(self):
        count = 0

        # svg is the root element (SvgDocumentElement)
        for elem in self.svg.iter():
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

    def remap_ids_in_clone(self, clone):
        old_to_new = {}

        # 1. Assign new IDs
        for el in clone.iter():
            old_id = el.get("id")
            if old_id:
                new_id = self.svg.get_unique_id(self.tag_name(el) or "X")
                old_to_new[old_id] = new_id
                el.set("id", new_id)

        # 2. Rewrite references inside the clone
        for el in clone.iter():
            for attr in ("clip-path", "mask", "fill", "stroke", "filter"):
                val = el.get(attr)
                if not val:
                    continue

                if val.startswith("url(#"):
                    ref_id = val[5:-1]
                    if ref_id in old_to_new:
                        el.set(attr, f"url(#{old_to_new[ref_id]})")


    def expand_all_uses(self, node=None, visited=None):
        """
        Deterministically expand ALL <use> elements in the SVG using
        post-order traversal (children first, then the node).

        Guarantees:
        - Clones never contain <use>
        - Cycles are detected and skipped
        - Non-expandable <use> elements are removed
        - Only one traversal is needed
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
        self.add_node(wrapper, parent, idx)
        self.append_transform(wrapper, self.use_transform(node))

        # Attach clone
        self.add_node(clone, wrapper)

        # Remove original <use>
        self.remove_node(node, parent)

    def remove_filter_for_element(self, el):
        """
        Remove filter references and delete corresponding <filter> nodes.
        Returns number of filters removed.
        """
        removed = 0

        # Check direct filter attribute
        filter_ref = el.get("filter")
        if filter_ref:
            el.attrib.pop("filter", None)
            tag = self.tag_name(el)
            id = el.get("id")
            self.log(logging.WARNING, f"Removed filter from <{self.node_str(el)}>")
            return 1

        return 0
    
    def remove_mask_for_element(self, el):
        """
        Remove mask references and delete corresponding <mask> nodes.
        Returns number of masks removed.
        """
        removed = 0

        # Check direct mask attribute
        mask_ref = el.get("mask")
        if mask_ref:
            el.attrib.pop("mask", None)
            tag = self.tag_name(el)
            id = el.get("id")
            self.log(logging.WARNING, f"Removed mask from {self.node_str(el)}")
            return 1

        return 0

    def remove_pattern_for_element(self, el):
        """
        Replace pattern fill with mean color and remove the pattern reference.
        Returns number of patterns removed (0 or 1).
        """

        # Resolve referenced element
        ref, ref_id = self.ref_target(el, "fill")
        if ref is None:
            return 0

        # Only handle <pattern>, ignore other references
        tag = self.tag_name(ref)
        if tag != "pattern":
            return 0

        # Collect geometry nodes inside the pattern
        nodes = list(ref.iterchildren())

        # Compute mean color
        mean_color = self.pattern_mean_color(nodes) if nodes else "#000000"

        # Replace fill with solid color
        el.set("fill", mean_color)

        # Logging
        self.log(logging.WARNING,
                f"Replaced pattern '{ref_id}' with fill '{mean_color}' "
                f"on {self.node_str(el)}")

        return 1

    #def resolve_marker_for_element(self, el):
    #    markers = {
    #        "start": el.get("marker-start"),
    #        "mid":   el.get("marker-mid"),
    #        "end":   el.get("marker-end")
    #    }

    #    if not any(markers.values()):
    #        return 0

    #    count = 0

        # Extract path geometry
    #    pts = self.extract_vertices(el)

    #    for pos, marker_url in markers.items():
    #        if not marker_url:
    #            continue

    #        marker_id = self.url_to_id(marker_url)
    #        marker = self.find_node(marker_id)
    #        if marker is None:
    #            continue

            # Determine which vertices to use
    #        if pos == "start":
    #            indices = [0]
    #        elif pos == "end":
    #            indices = [len(pts)-1]
    #        else:  # mid
    #            indices = range(1, len(pts)-1)

    #        for idx in indices:
    #            self.expand_marker_instance(el, marker, pts[idx], idx)
    #            count += 1

        # Remove marker attributes
    #    for attr in ("marker-start", "marker-mid", "marker-end"):
    #        el.attrib.pop(attr, None)

    #    return count


    def resolve_references(self):
        grad_count = 0
        clip_count = 0
        filter_count = 0
        mask_count = 0
        pattern_count = 0

        for el in list(self.svg.iter()):
            tag = self.tag_name(el)

            match tag:
                case "path" | "rect" | "circle" | "ellipse":
                    grad_count += self.resolve_gradient_for_shape(el)
                    clip_count += self.resolve_clippath_for_shape(el)
                    filter_count += self.remove_filter_for_element(el)
                    mask_count += self.remove_mask_for_element(el)
                    pattern_count += self.remove_pattern_for_element(el)

                case "clippath":
                    clip_count += self.resolve_clippath_for_shape(el)

                case _:
                    # Filters/masks can appear on ANY element
                    filter_count += self.remove_filter_for_element(el)
                    mask_count += self.remove_mask_for_element(el)
                    pattern_count += self.remove_pattern_for_element(el)

        if grad_count:
            self.log(logging.INFO, f"Normalized {grad_count} gradients")

        if clip_count:
            self.log(logging.INFO, f"Resolved {clip_count} clip paths")

        if filter_count:
            self.log(logging.INFO, f"Removed {filter_count} filters")

        if mask_count:
            self.log(logging.INFO, f"Removed {mask_count} masks")

        if pattern_count:
            self.log(logging.INFO, f"Replaced {pattern_count} patterns with solid fill color")

        self.log_defs()

    # endregion
            

    # region --- Viewbox Translation ---

    def compute_viewbox_translation(self):
        root = self.svg
        vb = root.get("viewBox")
        if not vb:
            return None

        x, y, w, h = map(float, vb.split())

        if x >= 0 and y >= 0:
            return None

        tx = -x if x < 0 else 0
        ty = -y if y < 0 else 0

        return Transform(f"translate({tx},{ty})")

    def translate_viewbox(self):
        root = self.svg
        t = self.compute_viewbox_translation()
        if not t:
            return

        for el in root.iter():
            tag = self.tag_name(el)

            if self.is_geometry(el):
                self.apply_transform_to_node(el, t)

            elif tag in ("linearGradient", "radialGradient"):
                self.apply_translation_to_gradient(el, t)

        # Rewrite viewBox to positive coordinates
        x, y, w, h = map(float, root.get("viewBox").split()) # type: ignore
        root.set("viewBox", f"0 0 {w} {h}")

        self.log(logging.INFO, "Translated viewbox to positive coordinates (geometry + gradients)")

    def apply_translation_to_gradient(self, grad, t):
        # Extract translation from matrix
        (a, c, e), (b, d, f) = t.matrix
        tx, ty = e, f

        # Only shift if gradientUnits is userSpaceOnUse
        if grad.get("gradientUnits") != "userSpaceOnUse":
            return

        def shift(attr, is_x):
            if attr in grad.attrib:
                try:
                    v = float(grad.get(attr))
                    grad.set(attr, str(v + (tx if is_x else ty)))
                except ValueError:
                    pass

        # Linear gradient
        shift("x1", True)
        shift("y1", False)
        shift("x2", True)
        shift("y2", False)

        # Radial gradient
        shift("cx", True)
        shift("cy", False)
        shift("fx", True)
        shift("fy", False)

        # Compose gradientTransform with translation
        try:
            base = Transform(grad.get("gradientTransform") or "")
        except Exception:
            base = Transform()

        combined = t @ base
        grad.set("gradientTransform", str(combined))

    # endregion

    # region --- Simplify Geometry ---

    
    def combine_paths(self, paths):
        if not paths:
            return None

        # Concatenate path data
        d = " ".join(p.get("d") for p in paths if p is not None)

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

    def empty_path(self):
        p = inkex.PathElement()
        p.set("d", "")
        return p


    def convert_to_path(self, node, transform=None, replace_node=False):
            """Convert any geometry node to a path element."""
            tag = node.tag

            if tag == inkex.addNS('path', 'svg'):
                # Parse existing path, preserving all subpaths
                p = node.path.to_absolute()

                self.log(logging.DEBUG,
                    f"[CTP] BEFORE transform: id={node.get('id')} "
                    f"fill-rule={node.get('fill-rule')} "
                    f"style={node.get('style')} "
                    f"subpaths={len(node.path.to_absolute())}"
                )


                # Apply transform safely
                if transform is not None:
                    p = p.transform(transform)

                self.log(logging.DEBUG,
                    f"[CTP] AFTER transform: id={node.get('id')} "
                    f"fill-rule={node.get('fill-rule')} "
                    f"style={node.get('style')} "
                    f"subpaths={len(p)}"
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

                self.log(logging.DEBUG,
                    f"[CTP] NEW_NODE: id={new_node.get('id')} "
                    f"fill-rule={new_node.get('fill-rule')} "
                    f"style={new_node.get('style')} "
                    f"subpaths={len(new_node.path)}"
                )


                return new_node

            if tag == inkex.addNS('rect', 'svg'):
                return self.rect_to_path(node, transform=transform, replace_node=replace_node)

            if tag == inkex.addNS('circle', 'svg'):
                return self.circle_to_path(node, transform=transform, replace_node=replace_node)

            if tag == inkex.addNS('ellipse', 'svg'):
                return self.ellipse_to_path(node, transform=transform, replace_node=replace_node)

            if tag in (inkex.addNS('polygon', 'svg'), inkex.addNS('polyline', 'svg')):
                return self.poly_to_path(node, transform=transform, replace_node=replace_node)

            if tag == inkex.addNS('line', 'svg'):
                return self.line_to_path(node, transform=transform, replace_node=replace_node)

            # Unsupported geometry → ignore
            return None

    def circle_to_path(self, node, transform=None, replace_node=True):
        # Extract geometry
        cx = float(node.get("cx", "0"))
        cy = float(node.get("cy", "0"))
        r  = float(node.get("r",  "0"))

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

        # Replace <circle> with <path>
        if replace_node:
            parent, idx = self.parent_of(node)
            self.remove_node(node)
            self.add_node(new_node, parent, idx)

        return new_node


    def rect_to_path(self, node, transform=None, replace_node=True):
        # Extract geometry
        x = float(node.get("x", "0"))
        y = float(node.get("y", "0"))
        w = float(node.get("width",  "0"))
        h = float(node.get("height", "0"))

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

        # Replace <rect> with <path>
        if replace_node:
            parent, idx = self.parent_of(node)
            self.remove_node(node)
            self.add_node(new_node, parent, idx)

        return new_node

        
    def ellipse_to_path(self, node, transform=None, replace_node=True):
        # Extract geometry
        cx = float(node.get("cx", "0"))
        cy = float(node.get("cy", "0"))
        rx = float(node.get("rx", "0"))
        ry = float(node.get("ry", "0"))

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

        # Replace <ellipse> with <path>
        if replace_node:
            parent, idx = self.parent_of(node)
            self.remove_node(node)
            self.add_node(new_node, parent, idx)

        return new_node

    def poly_to_path(self, node, transform=None, close=False, replace_node=True):
        points = node.get("points")
        if not points:
            return

        coords = [float(v) for v in re.split(r"[ ,]+", points.strip()) if v]
        if len(coords) < 2:
            return

        # Build path commands
        d = []
        for i in range(0, len(coords), 2):
            x, y = coords[i], coords[i + 1]
            if i == 0:
                d.append(f"M {x},{y}")
            else:
                d.append(f"L {x},{y}")

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

        # Replace original node
        if replace_node:
            parent, idx = self.parent_of(node)
            self.remove_node(node)
            self.add_node(new_node, parent, idx)

        return new_node

    def line_to_path(self, node, transform=None, replace_node=True):
        x1 = float(node.get("x1", "0"))
        y1 = float(node.get("y1", "0"))
        x2 = float(node.get("x2", "0"))
        y2 = float(node.get("y2", "0"))

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

        # Replace original node
        if replace_node:
            parent, idx = self.parent_of(node)
            self.remove_node(node)
            self.add_node(new_node, parent, idx)

        return new_node
        
    def replace_unsupported_shapes(self, node=None):
        count = 0

        # Modern inkex root access
        if node is None:
            node = self.svg

        # Iterate over a snapshot because nodes may be replaced
        for el in list(node.iter()):
            tag = self.tag_name(el)

            # Skip paint servers only
            match tag:
                case "linearGradient" | "radialGradient" | "pattern" | "filter" | "marker":
                    continue

                # Rounded rect → path
                case "rect":
                    rx = float(el.get("rx", "0") or "0")
                    ry = float(el.get("ry", "0") or "0")
                    if rx != 0 or ry != 0:
                        self.rect_to_path(el)
                        count += 1
                    continue

                # polyline → path
                case "polyline":
                    self.poly_to_path(el, close=False)
                    count += 1
                    continue

                # polygon → path
                case "polygon":
                    self.poly_to_path(el, close=True)
                    count += 1
                    continue

                # line → path
                case "line":
                    self.line_to_path(el)
                    count += 1
                    continue

                case _:
                    continue

        if count:
            self.log(logging.DEBUG, f"Replaced {count} unsupported shapes by paths")

        
    def apply_transform_to_gradients_used_by(self, node, T):
        """
        GT7-safe: gradients are already userSpaceOnUse and chain-resolved.
        We only need to re-express them in the flattened coordinate system.
        """

        count = 0

        fill = node.get("fill")
        stroke = node.get("stroke")

        for paint in (fill, stroke):
            if not paint or not paint.startswith("url(#"):
                continue

            grad_id = paint[5:-1]

            # Modern inkex API: use svg.getElementById()
            grad = self.find_node(grad_id)
            if grad is None:
                continue

            self.log(logging.DEBUG,
                f"apply_transform_to_gradients_used_by({node.get('id')}): "
                f"paint={paint}, grad_id={grad_id}, T={T}"
            )

            # GT7-safe: no gradientTransform allowed
            grad.attrib.pop("gradientTransform", None)

            # Only userSpaceOnUse is supported for GT7
            if grad.get("gradientUnits", "userSpaceOnUse") != "userSpaceOnUse":
                continue

            self.log(logging.DEBUG,
                f"apply_transform_to_gradients_used_by: applying CTM to gradient {grad_id}"
            )

            self.log(logging.DEBUG,
                f"gradient BEFORE shape transform: "
                f"id={grad.get('id')} x1={grad.get('x1')} y1={grad.get('y1')} "
                f"x2={grad.get('x2')} y2={grad.get('y2')} T={T.matrix}"
            )

            # Apply CTM to gradient geometry
            self.apply_transform_to_gradient(grad, T)

            self.log(logging.DEBUG,
                f"gradient AFTER shape transform: "
                f"id={grad.get('id')} x1={grad.get('x1')} y1={grad.get('y1')} "
                f"x2={grad.get('x2')} y2={grad.get('y2')} T={T.matrix}"
            )

            count += 1

        return count
    
    def apply_transform_to_clippath_used_by(self, node, T):
        self.log(logging.DEBUG,
            f"id={self.node_str(node)} clip-path={node.get('clip-path')} T={T}"
        )

        clip = node.get("clip-path")
        self.log(logging.DEBUG, f"raw clip-path={repr(clip)}")

        if not clip or not clip.startswith("url(#"):
            return 0

        cp_id = clip[5:-1]
        self.log(logging.DEBUG, f"parsed cp_id={repr(cp_id)}")

        cp = self.find_node(cp_id)

        self.log(logging.DEBUG, f"getElementById({repr(cp_id)}) → {cp}")

        if cp is None:
            return 0

        # Flatten clipPath using the shape's transform
        self.apply_transform_to_clippath(cp, T)

        return 1
    
    def apply_transform_to_clippath(self, cp, M_cp):
        """
        Apply the shape's transform chain to a resolved clipPath.
        After resolve_clippath(), cp contains exactly one <path> child
        with all geometry already flattened.
        """

        # 1. Compute final transform for this clipPath
        t = cp.get("transform")
        M_final = M_cp @ Transform(t) if t else M_cp

        # 2. Apply transform to the single geometry node
        for child in cp:
            if self.is_geometry(child, gt7_supported=False):
                self.apply_transform_to_node(child, M_final)
                child.attrib.pop("transform", None)

        # 3. Remove transform from the clipPath itself
        cp.attrib.pop("transform", None)

        return 1



    def apply_transform_to_node(self, node, transform):
        tag = self.tag_name(node)

        self.log(logging.DEBUG,
            f"{self.node_str(node)}, transform={transform} "
        )


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

        except Exception as e:
            self.log(logging.WARNING, f"Node transform failed for {self.node_str(node)} ")
            self.log(logging.WARNING, f"tag={node.tag} attrib={dict(node.attrib)}")
            
            self.log(logging.WARNING, f"exception: {type(e).__name__}: {e}")
            self.log(logging.WARNING, traceback.format_exc())
            return (0, node)

        return (1, node)
        
    def apply_all_transforms(self, node=None, parent_transform=None):
        # Modern inkex root access
        if node is None:
            node = self.svg
            self.cleanup_defs()
            self.log_defs()

        if parent_transform is None:
            parent_transform = Transform()

        count = 0

        # Modern tag resolution
        tag = self.tag_name(node)

        # Skip non-geometry paint servers
        if tag in ("linearGradient", "radialGradient", "pattern",
                   "filter", "marker", "stop", "clipPath"):
            return count

        # Parse local transform safely
        try:
            local_transform = Transform(node.get("transform"))
        except Exception:
            local_transform = Transform()

        # Combine CTMs
        combined = parent_transform @ local_transform

        self.log(logging.DEBUG,
            f"id={node.get('id')} "
            f"parent={parent_transform.matrix} "
            f"local={local_transform.matrix} "
            f"combined={combined.matrix}"
        )

        # Recurse into children
        for child in list(node):
            count += self.apply_all_transforms(child, combined)

        # Apply CTM to geometry
        counted, node = self.apply_transform_to_node(node, combined)
        count += counted

        # Apply CTM to gradients referenced by this node
        count += self.apply_transform_to_gradients_used_by(node, combined)

        count+= self.apply_transform_to_clippath_used_by(node, combined)

        # Remove transform attribute after flattening
        node.attrib.pop("transform", None)

        return count
        
    def flatten_group(self, g):
        # Parse group transform safely
        try:
            g_transform = Transform(g.get("transform"))
        except Exception:
            g_transform = Transform()

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

            # Apply group transform to child
            self.append_transform(child, g_transform)

            # Reparent child using modern DOM helpers
            g.remove(child)
            self.add_node(child, parent, idx)
            idx += 1

        # Remove the now-empty group
        self.remove_node(g, parent)
        
    def remove_all_groups(self, node=None):
        # Modern inkex root access
        if node is None:
            node = self.svg

        # Recurse into children first (post‑order traversal)
        for child in list(node):
            self.remove_all_groups(child)

        # Flatten this group if it *is* a group
        if self.tag_name(node) == "g":
            self.flatten_group(node)

    def group_by_common_presentation_attributes(self, node=None):
        if node is None:
            node = self.svg

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

            base_attrs = {
                k: v for k, v in base.attrib.items()
                if k in self.PRESENTATION_ATTRS
            }
            if not base_attrs:
                i += 1
                continue

            run = [base]
            j = i + 1
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

            if len(run) < 2:
                i += 1
                continue

            parent = node
            wrapper = inkex.Group()

            parent_index = parent.index(base)
            self.add_node(wrapper, parent, parent_index)

            for el in run:
                parent.remove(el)
                self.add_node(el, wrapper)

                for k in base_attrs.keys():
                    el.attrib.pop(k, None)

            for k, v in base_attrs.items():
                wrapper.set(k, v)

            children = list(node)
            i = parent_index + 1

        # remove redundant attributes from geometry nodes
        self.remove_redundant_attributes()

    # endregion

    # region --- Cleanup SVG Tree ----
        
    def collect_referenced_ids(self):
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

    
    
    def cleanup_defs(self):

        defs = self.svg.find(".//{http://www.w3.org/2000/svg}defs")
        if defs is None:
            return

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
                        f"[DEFS] Removing unused defs child id={cid} tag={child.tag_name}"
                    )
                    defs.remove(child)
                    removed_any = True
                else:
                    self.log(logging.DEBUG,
                        f"[DEFS] Keeping defs child id={cid}"
                    )


    def log_defs(self):
        defs = self.find_node("defs")
        if defs is None:
            self.log(logging.DEBUG, "No <defs> element found")
            return

        self.log(logging.DEBUG, "--- DEFS CONTENT ---")
        for child in defs:
            tag = self.tag_name(child)
            cid = child.get("id")
            self.log(logging.DEBUG, f"defs child: {self.node_str(child)}")
        self.log(logging.DEBUG, "--- END DEFS ---")


    def remove_unreferenced_referenceables(self):
        referenced = self.collect_referenced_ids()

        # Namespace-safe lookup
        ns = {"svg": "http://www.w3.org/2000/svg"}

        removed_any = True

        while removed_any:

            removed_any = False

            # Find all elements with an id
            for el in self.svg.xpath(".//svg:*[@id]", namespaces=ns):
                cid = el.get("id")
                tag = self.tag_name(el)

                # Only remove referenceable elements
                if tag not in self.REFERENCEABLE_TAGS:
                    continue

                # Keep referenced ones
                if cid in referenced:
                    continue

                # Remove unreferenced ones
                parent = el.getparent()
                if parent is not None:
                    self.log(logging.DEBUG,
                        f"[CLEANUP] Removing unreferenced {tag} id={cid}"
                    )
                    parent.remove(el)
                    removed_any=True

    def clean_stroke_attributes(self):
        count = 0

        # Modern inkex root access
        for elem in self.svg.iter():
            stroke = elem.get("stroke")
            stroke_width = elem.get("stroke-width")

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
                    elem.attrib.pop(attr, None)
                count += 1

        if count:
            self.log(logging.INFO, f"Cleaned {count} invisible strokes")

    def round_all_coordinates(self, node=None):
        # Modern inkex root access
        if node is None:
            node = self.svg

        # Attributes that may contain floats
        float_attrs = (
            "x", "y", "cx", "cy", "r", "rx", "ry",
            "width", "height", "stroke-width",
            "x1", "y1", "x2", "y2",          # gradients
            "fx", "fy",                      # radial gradients
        )

        digits = self.COORDINATE_ROUNDING_PRECISION

        for el in node.iter():

            # 1. Path data
            if "d" in el.attrib:
                el.set("d", self.round_floats_in_string(el.get("d"), digits=digits))

            # 2. Generic float attributes
            for attr in float_attrs:
                if attr in el.attrib:
                    el.set(attr, self.round_floats_in_string(el.get(attr), digits=digits))

            # 3. Transform attributes (elements + gradients)
            if "transform" in el.attrib:
                el.set("transform", self.round_floats_in_string(el.get("transform"), digits=digits))

            if "gradientTransform" in el.attrib:
                el.set("gradientTransform", self.round_floats_in_string(el.get("gradientTransform"), digits=digits))


    def remove_comments(self, node=None):
        if node is None:
            node = self.svg

        for el in list(node):
            tag = el.tag

            if isinstance(tag, str) and tag.endswith("namedview"):
                continue

            if isinstance(el, inkex.etree._Comment):
                node.remove(el)
                continue

            self.remove_comments(el)

            
    def remove_redundant_attributes(self, node=None, inherited=None):
        if node is None:
            node = self.svg

        tag = node.tag
        if isinstance(tag, str) and tag.endswith("namedview"):
            return

        if inherited is None:
            inherited = {}

        local_inherited = dict(inherited)

        node_attrs = {
            k: v for k, v in node.attrib.items()
            if k in self.PRESENTATION_ATTRS
        }

        for attr, val in list(node_attrs.items()):
            if attr in inherited and inherited[attr] == val:
                del node.attrib[attr]
                del node_attrs[attr]

        for attr, default in self.DEFAULTS.items():
            if node.get(attr) == default:
                del node.attrib[attr]
                node_attrs.pop(attr, None)

        if node.get("stroke") == "none" or node.get("stroke-width") == "0":
            for a in self.STROKE_ATTRS:
                node.attrib.pop(a, None)
            node_attrs = {
                k: v for k, v in node_attrs.items()
                if k not in self.STROKE_ATTRS
            }

        local_inherited.update(node_attrs)

        for child in node:
            self.remove_redundant_attributes(child, local_inherited)

    def remove_non_gt7_attributes(self, node=None):
        if node is None:
            node = self.svg

        tag = self.tag_name(node)
        id = node.get("id")

        self.log(logging.DEBUG,f"Inspecting node {tag} id={id}")

                # Recurse
        for child in node:
            self.remove_non_gt7_attributes(child)

        # Skip non geometry nodes
        if not self.is_geometry(node):
            self.log(logging.DEBUG,f"Ignoring node {self.node_str(node)}")
            return

        # Remove all attributes not in whitelist
        for attr in list(node.attrib.keys()):
            self.log(logging.DEBUG,f"Inspecting atribute {tag} id={id} attr={attr}")

            if attr not in self.GT7_ATTRS:
                self.log(logging.DEBUG,f"Removed attribute {attr} from node {tag} id={id}")
                node.attrib.pop(attr, None)

    def remove_non_gt7_elements(self):
        """
        Remove editor-specific and unsafe elements/attributes:
        - <script>
        - Inkscape/Sodipodi elements
        - Inkscape/Sodipodi attributes
        """

        # --- 1. Remove all <script> elements ---
        for el in list(self.svg.iter()):
            tag = self.tag_name(el)
            if tag == "script":
                parent = el.getparent()
                if parent is not None:
                    parent.remove(el)

        # --- 2. Remove editor-specific elements (namespaced) ---
        for el in list(self.svg.iter()):
            tag = self.tag_name(el)
            if tag.startswith("{http://www.inkscape.org/namespaces/inkscape}") or \
            tag.startswith("{http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"):
                parent = el.getparent()
                if parent is not None:
                    parent.remove(el)

        # --- 3. Strip editor-specific attributes from remaining elements ---
        for el in self.svg.iter():
            attribs = list(el.attrib.items())
            for name, _ in attribs:
                if name.startswith("{http://www.inkscape.org/namespaces/inkscape}") or \
                name.startswith("{http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"):
                    del el.attrib[name]


    def compress_output(self):
        self.remove_comments()
        self.group_by_common_presentation_attributes()
        self.round_all_coordinates()
        self.remove_non_gt7_attributes()
        self.remove_non_gt7_elements()
        
        
        
        self.log(logging.INFO, f"Compressed output")

    # endregion

    # region --- Resolving Gradients ---

    import math

    def pca_color_axis(self, colors):
        """
        colors: list of (r, g, b) floats in [0, 1]
        returns: (min_color, max_color)
        """

        # --- Step 1: convert to float vectors ---
        # colors is already [(r,g,b), ...] in your pipeline

        # --- Step 2: compute mean ---
        n = len(colors)
        mean = [
            sum(c[i] for c in colors) / n
            for i in range(3)
        ]

        # --- Step 3: center colors ---
        centered = [
            (c[0] - mean[0], c[1] - mean[1], c[2] - mean[2])
            for c in colors
        ]

        # --- Step 4: covariance matrix (3×3) ---
        cov = [[0.0]*3 for _ in range(3)]
        for cx, cy, cz in centered:
            cov[0][0] += cx*cx
            cov[0][1] += cx*cy
            cov[0][2] += cx*cz
            cov[1][0] += cy*cx
            cov[1][1] += cy*cy
            cov[1][2] += cy*cz
            cov[2][0] += cz*cx
            cov[2][1] += cz*cy
            cov[2][2] += cz*cz

        for i in range(3):
            for j in range(3):
                cov[i][j] /= (n - 1)

        # --- Step 5: eigen decomposition (power iteration) ---
        def power_iteration(matrix, iterations=50):
            v = [1.0, 1.0, 1.0]  # initial vector
            for _ in range(iterations):
                # multiply matrix * v
                mv = [
                    matrix[0][0]*v[0] + matrix[0][1]*v[1] + matrix[0][2]*v[2],
                    matrix[1][0]*v[0] + matrix[1][1]*v[1] + matrix[1][2]*v[2],
                    matrix[2][0]*v[0] + matrix[2][1]*v[1] + matrix[2][2]*v[2],
                ]
                # normalize
                norm = math.sqrt(mv[0]**2 + mv[1]**2 + mv[2]**2)
                v = [mv[i]/norm for i in range(3)]
            return v

        axis = power_iteration(cov)

        # --- Step 6: project colors onto axis ---
        projections = []
        for c in colors:
            # dot product with axis
            proj = c[0]*axis[0] + c[1]*axis[1] + c[2]*axis[2]
            projections.append(proj)

        # --- Step 7: find min/max projected colors ---
        min_index = projections.index(min(projections))
        max_index = projections.index(max(projections))

        return colors[min_index], colors[max_index]

    def parse_color_rgb(self, col):
        # Accepts #RRGGBB or rgb(r,g,b)
        col = col.strip()
        if col.startswith("#"):
            r = int(col[1:3], 16) / 255.0
            g = int(col[3:5], 16) / 255.0
            b = int(col[5:7], 16) / 255.0
            return (r, g, b)
        if col.startswith("rgb"):
            nums = col[col.find("(")+1:col.find(")")].split(",")
            r, g, b = [int(x)/255.0 for x in nums]
            return (r, g, b)
        return (0, 0, 0)

    def rgb_to_hex(self, c):
        r = int(c[0]*255) 
        g = int(c[1]*255)
        b = int(c[2]*255)
        return f"#{r:02x}{g:02x}{b:02x}"
    
    def fit_line_least_squares(self, pts):
        """
        Fit a line through a list of (x,y) points using least squares.
        Returns (x1, y1, x2, y2) = endpoints of the fitted line.
        """

        # Fallback if no geometry
        if not pts:
            return (0, 0, 1, 0)

        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]

        n = len(pts)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n

        # slope a = Σ((x-mean_x)(y-mean_y)) / Σ((x-mean_x)^2)
        num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
        den = sum((xs[i] - mean_x)**2 for i in range(n))
        a = num / den if den != 0 else 0.0
        b = mean_y - a * mean_x

        # projection helper
        def project(px, py):
            vx, vy = 1.0, a
            norm2 = vx*vx + vy*vy
            t = ((px - 0)*vx + (py - b)*vy) / norm2
            x_proj = t * vx
            y_proj = a * x_proj + b
            return (x_proj, y_proj)

        # project all points
        projections = [project(px, py) for px, py in pts]

        # find min/max along the fitted axis
        x1, y1 = projections[0]
        x2, y2 = projections[0]

        for px, py in projections:
            if px < x1:
                x1, y1 = px, py
            if px > x2:
                x2, y2 = px, py

        return (x1, y1, x2, y2)

    def mesh_points(self, stop, origin_x, origin_y):
        path = stop.get("path")
        if not path:
            return []

        tokens = path.strip().split()
        pts = []
        cx, cy = origin_x, origin_y  # current position

        i = 0
        while i < len(tokens):
            if tokens[i].lower() == "c":
                # c dx1,dy1 dx2,dy2 dx3,dy3
                if i + 3 < len(tokens):
                    dx, dy = tokens[i+3].split(",")
                    cx += float(dx)
                    cy += float(dy)
                    pts.append((cx, cy))
                    i += 4
                else:
                    break
            else:
                i += 1

        return pts


    
    def make_stop(self, offset, rgb):
        """
        Create a <stop> element with GT7-safe attributes.
        rgb is a tuple (r, g, b) in [0..1].
        """
        stop = inkex.elements.Stop()  # type: ignore
        stop.set("offset", str(offset))
        stop.set("stop-color", self.rgb_to_hex(rgb))
        return stop

    def resolve_mesh_gradient_chain(self, mg):
        """
        Resolve chained meshgradients:
        - follow xlink:href chain
        - inherit missing attributes
        - inherit stops and coordinates
        - return a flattened meshgradient clone
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
            href, ref_id = self.ref_target(current)
            if href is None:
                break

            parent = self.find_node(ref_id)
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


    def replace_mesh_gradient(self, mg):
        ns = {"svg": "http://www.w3.org/2000/svg"}

        tag = self.tag_name(mg)
        id = mg.get("id")

        self.log(logging.WARNING,f"Replacing {tag} id={id} with linearGradient")

        # --- Step 0: resolve chained attributes ---
        mg = self.resolve_mesh_gradient_chain(mg)
        
        # --- Step 1: collect stop colors + positions ---
        colors = []
        pts = []

        origin_x = float(mg.get("x", 0))
        origin_y = float(mg.get("y", 0))

        for stop in mg.xpath(".//svg:stop", namespaces=ns):
            
            points = self.mesh_points(stop, origin_x, origin_y)
            pts.extend(points)

            col = stop.get("stop-color")
            if col:
                colors.append(self.parse_color_rgb(col))

        if not colors:
            colors = [(0,0,0), (1,1,1)]
        if not pts:
            pts = [(0,0), (1,0)]

        # --- Step 2: PCA dominant color axis ---
        c_min, c_max = self.pca_color_axis(colors)

        # --- Step 3: least-squares axis ---
        x1, y1, x2, y2 = self.fit_line_least_squares(pts)

        # --- Step 4: remove original meshgradient ---
        gradientUnits = mg.get("gradientUnits")
        transform = mg.get("gradientTransform")

        # --- Step 5: create new linearGradient ---
        lg = inkex.elements.LinearGradient()  # type: ignore
        lg.set("x1", str(x1))
        lg.set("y1", str(y1))
        lg.set("x2", str(x2))
        lg.set("y2", str(y2))

        if gradientUnits is not None:
            lg.set("gradientUnits", gradientUnits)

        if transform is not None:
            lg.set("gradientTransform", transform)

        # --- Step 6: add stops ---
        lg.add(self.make_stop(0, c_min))
        lg.add(self.make_stop(1, c_max))

        lg.attrib.pop("id", None)

        return lg



    def ensure_defs(self):
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

    def parse_and_sort_stops(self, grad):
        # Use the gradient's own namespace instead of hard‑coding SVG ns
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


    def strip_alpha_from_color(self, stop):
        # Global toggle
        if not self.STRIP_ALPHA_FROM_GRADIENTS:
            return

        # Remove stop-opacity entirely
        stop.attrib.pop("stop-opacity", None)

        col = stop.get("stop-color")
        if not col:
            return

        col = col.strip()

        # rgba(r,g,b,a)
        if col.startswith("rgba"):
            inner = col[col.find("(")+1 : col.find(")")]
            parts = [p.strip() for p in inner.split(",")]
            if len(parts) >= 3:
                stop.set("stop-color", f"rgb({parts[0]},{parts[1]},{parts[2]})")
            return

        # hsla(h,s,l,a)
        if col.startswith("hsla"):
            inner = col[col.find("(")+1 : col.find(")")]
            parts = [p.strip() for p in inner.split(",")]
            if len(parts) >= 3:
                stop.set("stop-color", f"hsl({parts[0]},{parts[1]},{parts[2]})")
            return

        # rgb() or hsl() already fine → nothing to do
        return

    def reduce_to_first_and_last_stop(self, grad, parsed):
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
        self.strip_alpha_from_color(first)
        self.strip_alpha_from_color(last)

        # Remove all intermediate stops
        for _, s in parsed[1:-1]:
            if s not in (first, last):
                self.remove_node(s, grad)

        return first, last

    def normalize_gradient_stops_and_colors(self, grad):
        # Parse + sort stops (svg‑API safe)
        parsed = self.parse_and_sort_stops(grad)
        if not parsed:
            return

        # Reduce to first + last stop (alpha stripping controlled by global flag)
        self.reduce_to_first_and_last_stop(grad, parsed)

        # Logging
        gid = grad.get("id", "")
        self.log(logging.DEBUG,
                 f"Normalized gradient stops for id={gid} "
                 f"(first+last only, alpha stripped={self.STRIP_ALPHA_FROM_GRADIENTS})")

    def normalize_gradient_units(self, grad, shape):
        """
        Convert objectBoundingBox gradients into userSpaceOnUse by:
        1. Computing the shape's user-space bounding box
        2. Computing the shape's full transform chain
        3. Building T_final = T_shape ∘ translate(bx,by) ∘ scale(bw,bh)
        4. Applying T_final to all gradient coordinates
        5. Removing gradientTransform
        6. Setting gradientUnits=userSpaceOnUse
        """

        # 0. Already userSpaceOnUse → nothing to do
        if grad.get("gradientUnits") == "userSpaceOnUse":
            return

        gid = grad.get("id", "")
        self.log(logging.DEBUG,
                 f"Converting gradient id={gid} from objectBoundingBox → userSpaceOnUse")

        # 1. Compute bounding box in user space
        bbox = self.compute_shape_bbox(shape)
        if not bbox:
            self.log(logging.WARNING,
                     f"WARNING: Could not compute bbox for shape using gradient id={gid}")
            grad.set("gradientUnits", "userSpaceOnUse")
            return

        bx, by, bw, bh = bbox

        # 2. Compute full transform chain of the shape
        T_shape = self.compute_full_transform(shape)

        # 3. Build bbox normalization transform
        T_bbox = Transform(f"translate({bx},{by})") @ Transform(f"scale({bw},{bh})")

        # 4. Combine transforms
        T_final = T_shape @ T_bbox

        # 5. Apply transform to gradient coordinates
        self.apply_transform_to_gradient(grad, T_final)

        # 6. Remove gradientTransform (already baked in)
        grad.attrib.pop("gradientTransform", None)

        # 7. Force userSpaceOnUse
        grad.set("gradientUnits", "userSpaceOnUse")

        self.log(logging.DEBUG,
                 f"Gradient id={gid} converted to userSpaceOnUse")

    def apply_gradient_transform_matrix(self, grad, t):
        tag = self.tag_name(grad)

        (a, c, e), (b, d, f) = t.matrix

        if tag == "linearGradient":
            x1 = float(grad.get("x1", "0"))
            y1 = float(grad.get("y1", "0"))
            x2 = float(grad.get("x2", "0"))
            y2 = float(grad.get("y2", "0"))

            grad.set("x1", str(a * x1 + c * y1 + e))
            grad.set("y1", str(b * x1 + d * y1 + f))
            grad.set("x2", str(a * x2 + c * y2 + e))
            grad.set("y2", str(b * x2 + d * y2 + f))

        elif tag == "radialGradient":
            cx = float(grad.get("cx", "0"))
            cy = float(grad.get("cy", "0"))
            r  = float(grad.get("r", "0"))

            # Transform center
            grad.set("cx", str(a * cx + c * cy + e))
            grad.set("cy", str(b * cx + d * cy + f))

            # --- radius logic ---
            # Pure translation → do NOT scale r
            if a == 1 and d == 1 and b == 0 and c == 0:
                return

            # Uniform scale → scale r
            if b == 0 and c == 0 and a == d:
                grad.set("r", str(r * abs(a)))
                return

            # Non-uniform scale → GT7-unsafe (ellipse)
            # You may choose to warn, clamp, or approximate
            # For now: leave r unchanged
            return


    def clone_gradient(self, grad):
        # Determine gradient type using svg‑API tag resolution
        tag = self.tag_name(grad)

        if tag == "linearGradient":
            new_grad = inkex.elements.LinearGradient() # type: ignore
        elif tag == "radialGradient":
            new_grad = inkex.elements.RadialGradient() # type: ignore
        elif tag == "meshgradient":
            return self.replace_mesh_gradient(grad)
        else:
            # Fallback: generic element with same tag + same nsmap
            new_grad = inkex.etree.Element(grad.tag, nsmap=grad.nsmap)

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

    def compute_full_transform(self, node):
        """Accumulate transforms from node up to the root (same order as apply_all_transforms)."""

        transforms = []
        current = node

        # Collect transforms bottom → top
        while current is not None:
            try:
                local = Transform(current.get("transform"))
            except Exception:
                local = Transform()
            transforms.append(local)
            current = current.getparent()

        # Combine in correct order: parent @ local
        T_full = Transform()
        for t in reversed(transforms):
            T_full = T_full @ t

        return T_full

    def resolve_gradient_chain(self, grad):
        """
        Resolve an entire gradient chain:
        - flatten href inheritance (attributes + stops)
        - resolve gradientTransform according to SVG spec:
            * If child has gradientTransform → ignore all parent transforms
            * If child has none → inherit first parent gradientTransform
        """

        g = grad
        seen = set()

        gid = grad.get("id", "")
        self.log(logging.DEBUG, f"Resolving chain for gradient clone (temp id={gid})")

        # SVG namespace for stop lookup
        svg_ns = self.svg.nsmap.get(None, "http://www.w3.org/2000/svg")

        while True:
            gid = g.get("id")
            self.log(logging.DEBUG, f"  Visiting gradient {gid}")

            # Cycle detection
            if gid in seen:
                self.log(logging.DEBUG, "    STOP: cycle detected")
                break
            if gid:
                seen.add(gid)

            # Debug: child/parent chain
            self.log(logging.DEBUG, f"[CHAIN] Child gradient id={grad.get('id')}")

            # Follow href chain (both plain and xlink)
            ref, ref_id = self.ref_target(g)
            
            self.log(logging.DEBUG, f"[CHAIN] Child href → {ref_id}")

            if ref is None:
                self.log(logging.DEBUG, f"ERROR: referenced gradient {ref_id} not found")
                break

            self.log(logging.DEBUG, f"[CHAIN] Parent gradient found: id={ref.get('id')}")

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
                self.log(logging.DEBUG, f"[CHAIN] Inheriting {len(parent_stops)} stops from parent {ref.get('id')}")
                for stop in parent_stops:
                    self.add_node(copy.deepcopy(stop), grad)

            # Drop href on the working clone
            grad.attrib.pop("href", None)
            grad.attrib.pop(f"{{{self.XLINK_NS}}}href", None)

            g = ref

        # --- SPEC RULE 3: choose final transform ---
        gt = grad.attrib.get("gradientTransform", None)
        if gt is None:
            self.log(logging.DEBUG, "  No gradientTransform found → identity")
            T_chain = Transform()
        else:
            self.log(logging.DEBUG, f"  Resolved gradientTransform found → {gt}")
            T_chain = Transform(gt)

        # Remove gradientTransform from the clone (baked into coords later)
        grad.attrib.pop("gradientTransform", None)
        self.log(logging.DEBUG, "  Removed gradientTransform attribute")

        gid = grad.get("id", "")
        self.log(logging.DEBUG, f"Resolved full gradient chain for id={gid}")

        return T_chain

        
    def resolve_gradient_for_shape(self, shape):
        """
        For each shape using url(#grad), create a flattened, GT7‑safe gradient:
        - clone original gradient (type + stops)
        - resolve href/gradientTransform chain (returns T_chain)
        - apply chain transform to gradient coordinates
        - normalize stops and units
        - strip internal references
        - register new gradient in <defs> and rewire shape
        """
        changed = 0

        # SVG namespace for stop lookup
        svg_ns = self.svg.nsmap.get(None, "http://www.w3.org/2000/svg")

        for attr in ("fill", "stroke"):
            grad, grad_id = self.ref_target(shape, attr)
            
            self.log(logging.DEBUG, f"Shape = {self.node_str(shape)}")

            grad = self.find_node(grad_id)
            if grad is None:
                self.log(logging.DEBUG, f"{attr} reference {grad_id} not found in SVG tree")
                continue

            tag = self.tag_name(grad)
            
            if not tag in {"linearGradient", "radialGradient", "meshgradient"}:
                self.log(logging.DEBUG, f"Gradient {tag}: id={grad_id} not supported")
                continue

            self.log(
                logging.DEBUG,
                f"  Original gradient {grad_id}: "
                f"x1={grad.get('x1')} y1={grad.get('y1')} "
                f"x2={grad.get('x2')} y2={grad.get('y2')} "
                f"gt={grad.get('gradientTransform')}"
            )

            # clone original gradient definition
            new_grad = self.clone_gradient(grad)
            self.log(logging.DEBUG, f"  Cloned gradient has id={new_grad.get('id')}")

            # resolve chain on the cloned gradient, get chain transform
            T_chain = self.resolve_gradient_chain(new_grad)
            self.log(logging.DEBUG, f"[GRADIENT]   T_chain for {shape.get('id')}: {T_chain}")

            # apply chain transform (shape transform is handled later in apply_all_transforms)
            self.apply_gradient_transform_matrix(new_grad, T_chain)

            self.log(
                logging.DEBUG,
                f"  After chain resolution: "
                f"x1={new_grad.get('x1')} y1={new_grad.get('y1')} "
                f"x2={new_grad.get('x2')} y2={new_grad.get('y2')}"
            )

            # safety: no xlink:href left
            new_grad.attrib.pop("xlink:href", None)

            # normalize stops and units
            self.normalize_gradient_stops_and_colors(new_grad)
            self.normalize_gradient_units(new_grad, shape)

            self.log(
                logging.DEBUG,
                f"  After normalization: "
                f"x1={new_grad.get('x1')} y1={new_grad.get('y1')} "
                f"x2={new_grad.get('x2')} y2={new_grad.get('y2')}"
            )

            # ensure defs and register new gradient
            defs = self.ensure_defs()
            self.add_node(new_grad, defs)

            self.log(logging.DEBUG, f"  New gradient id={new_grad.get('id')} assigned to shape")

            # rewire shape to the new gradient id
            shape.set(attr, f"url(#{new_grad.get('id')})")
            changed += 1

        return changed
        
    def compute_shape_bbox(self, node):
        """Return (x, y, width, height) in user space for a single shape node."""
        tag = self.tag_name(node)
        T = self.compute_full_transform(node)

        # PATH
        if tag == "path":
            d = node.get("d")
            if not d:
                return None

            p = inkex.Path(d) # type: ignore
            bbox = p.bounding_box()  # svg‑API: returns BoundingBox(x_interval, y_interval)

            # Use bbox.minimum / bbox.maximum (Vector2d) → linter-safe
            min_pt = bbox.minimum   # Vector2d(x_min, y_min) # type: ignore
            max_pt = bbox.maximum   # Vector2d(x_max, y_max) # type: ignore

            # Compute the four corners
            pts = [
                T.apply_to_point((min_pt.x, min_pt.y)),
                T.apply_to_point((max_pt.x, min_pt.y)),
                T.apply_to_point((min_pt.x, max_pt.y)),
                T.apply_to_point((max_pt.x, max_pt.y)),
            ]

            xs = [pt[0] for pt in pts]
            ys = [pt[1] for pt in pts]

            min_x = min(xs)
            max_x = max(xs)
            min_y = min(ys)
            max_y = max(ys)

            return min_x, min_y, max_x - min_x, max_y - min_y

        pts = []

        # RECT
        if tag == "rect":
            x = float(node.get("x", 0))
            y = float(node.get("y", 0))
            w = float(node.get("width", 0))
            h = float(node.get("height", 0))

            pts = [
                (x,     y),
                (x + w, y),
                (x,     y + h),
                (x + w, y + h),
            ]

        # CIRCLE
        elif tag == "circle":
            cx = float(node.get("cx", 0))
            cy = float(node.get("cy", 0))
            r  = float(node.get("r", 0))

            pts = [
                (cx - r, cy),
                (cx + r, cy),
                (cx, cy - r),
                (cx, cy + r),
            ]

        # ELLIPSE
        elif tag == "ellipse":
            cx = float(node.get("cx", 0))
            cy = float(node.get("cy", 0))
            rx = float(node.get("rx", 0))
            ry = float(node.get("ry", 0))

            pts = [
                (cx - rx, cy),
                (cx + rx, cy),
                (cx, cy - ry),
                (cx, cy + ry),
            ]

        # POLYGON / POLYLINE
        elif tag in ("polygon", "polyline"):
            raw = node.get("points", "")
            if not raw.strip():
                return None

            # svg‑API: split on whitespace or commas
            coords = [float(v) for v in re.split(r"[ ,]+", raw.strip()) if v]
            pts = [(coords[i], coords[i + 1]) for i in range(0, len(coords), 2)]

        else:
            return None

        # Apply transform to all points (svg‑API)
        pts = [T.apply_to_point(pt) for pt in pts]

        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]

        min_x = min(xs)
        max_x = max(xs)
        min_y = min(ys)
        max_y = max(ys)

        return min_x, min_y, max_x - min_x, max_y - min_y

    def apply_transform_to_gradient(self, grad, T):
        """Apply a Transform to all gradient coordinate attributes."""
        self.log(
            logging.DEBUG,
            f"apply_transform_to_gradient BEFORE: "
            f"id={grad.get('id')} "
            f"x1={grad.get('x1')} y1={grad.get('y1')} "
            f"x2={grad.get('x2')} y2={grad.get('y2')} "
            f"T={T.matrix}"
        )

        def apply_point(x_attr, y_attr):
            """Apply transform to a coordinate pair if both attributes exist."""
            if x_attr in grad.attrib and y_attr in grad.attrib:
                x = float(grad.get(x_attr))
                y = float(grad.get(y_attr))
                x2, y2 = T.apply_to_point((x, y))
                grad.set(x_attr, str(x2))
                grad.set(y_attr, str(y2))

        tag = self.tag_name(grad)

        # LINEAR GRADIENT
        if tag == "linearGradient":
            apply_point("x1", "y1")
            apply_point("x2", "y2")

        # RADIAL GRADIENT
        elif tag == "radialGradient":
            apply_point("cx", "cy")
            apply_point("fx", "fy")

            # radius transforms as scale of unit vector
            if "r" in grad.attrib:
                r = float(grad.get("r"))
                rx, ry = T.apply_to_point((r, 0))
                grad.set("r", str((rx**2 + ry**2)**0.5))

        self.log(
            logging.DEBUG,
            f"apply_transform_to_gradient AFTER: "
            f"id={grad.get('id')} "
            f"x1={grad.get('x1')} y1={grad.get('y1')} "
            f"x2={grad.get('x2')} y2={grad.get('y2')}"
        )

    # endregion

    # region --- clipping ---
    

    def resolve_clippath(self, cp, transform = Transform()):
        self.log(logging.DEBUG, f"[CP] resolve_clippath id={cp.get('id')} M={transform}")
        self.log(logging.DEBUG, f"[CP]   children={[self.tag_name(c) for c in cp]}")


        # accumulate transform on <clipPath>
        if cp.get("transform"):
            t = Transform(cp.get("transform"))
        else:
            t = Transform()

        transform = transform @ t

        # unify children WITHOUT applying their clip-paths
        parts = [self.resolve_clippath_geometry(child, transform) for child in cp]
        geom = self.path_union(parts)

        # handle href / xlink:href on <clipPath> itself
        ref, ref_id = self.ref_target(cp)
        if ref is not None:
            ref_geom = self.resolve_clippath(ref, transform)
            geom = self.path_intersection(geom, ref_geom)

        return geom



    def resolve_clippath_geometry(self, node, M):
        self.log(logging.DEBUG, f"[CP]   resolve_clippath_geometry id={node.get('id')} tag={self.tag_name(node)} M={M}")
        self.log(logging.DEBUG, f"[CP]   is_geometry={self.is_geometry(node, gt7_supported=True)} clip-path={node.get('clip-path')}")

        # accumulate transform
        if node.get("transform"):
            t = Transform(node.get("transform"))
            M = M @ t

        tag = self.tag_name(node)

        # geometry → path
        if self.is_geometry(node, gt7_supported=True):
            geom = self.convert_to_path(node, M)

        # group → unify children
        elif tag == "g":
            parts = [self.resolve_clippath_geometry(child, M) for child in node]
            geom = self.path_union(parts)

        else:
            return self.empty_path()

        # apply clip-path to gemoetry (if any)
        cp = self.get_clippath(node)
        if cp is not None:
            clip_geom = self.resolve_clippath(cp, M)
            geom = self.path_intersection(geom, clip_geom)

        return geom

    def get_clippath(self, el):
        """
        Return the <clipPath> element referenced by el's clip-path attribute.
        Follow href chains. Do NOT resolve geometry.
        """

        # 1. Extract clip-path reference
        cp, cp_id = self.ref_target(el, "clip-path")

        if cp is None:
            return None
        
        #TODO - search for nested clipPath here!!!

        #TODO Why is this needed - clipPath chain needs to be resolved properly!
        # 3. Follow href chains inside <clipPath>
        #while True:
        #    href_target, href_id = self.ref_target(cp)
        #    if href_target is None:
        #        break
        #    cp = href_target

        return cp


    def resolve_clippath_for_shape(self, shape):
        """
        Resolve the clipPath referenced by 'shape' into a flattened path,
        wrap it into a new <clipPath>, insert into <defs>, and rewire shape.
        """

        # 1. Get structural <clipPath> element
        cp = self.get_clippath(shape)
        if cp is None:
            return 0

        # 2. Resolve clipPath geometry (two‑mode resolver)
        flattened = self.resolve_clippath(cp, Transform())
        if flattened is None:
            # No geometry → remove clip-path
            shape.attrib.pop("clip-path", None)
            return 0

        # 3. Wrap flattened geometry into a new <clipPath>
        #    (GT7-safe: only <path> children, no forbidden attributes)
        svg_ns = self.svg.nsmap.get(None, "http://www.w3.org/2000/svg")
        cp_new = inkex.etree.Element(f"{{{svg_ns}}}clipPath")

        # Copy flattened geometry into the new <clipPath>
        cp_new.append(flattened)
        cp_new.attrib.pop("id", None)

        # 4. Insert new <clipPath> into <defs> and assign new ID
        defs = self.ensure_defs()
        cp_clone = self.add_node(cp_new, defs)
        new_id = cp_clone.get("id")

        # 5. Rewire shape to use the new clipPath
        shape.set("clip-path", f"url(#{new_id})")

        return 1
    
    def remove_all_clippaths(self, node=None):
        """
        Post-order clipping pass.
        Mirrors apply_all_transforms() in structure and traversal.
        For each geometry node with clip-path="url(#id)", resolve the
        referenced <clipPath>, intersect geometry with the flattened
        clip geometry, replace the node's path data, and remove the
        clip-path attribute entirely.

        After traversal, remove all <clipPath> elements from <defs>.
        """

        count = 0

        # Entry point: start at root
        if node is None:
            node = self.svg

        tag = self.tag_name(node)

        # Skip paint servers and clipPath definitions themselves
        if tag in ("linearGradient", "radialGradient", "pattern",
                "filter", "marker", "stop"):
            return 0

        # --- 1. Recurse into children (post-order) ---
        for child in list(node):
            count += self.remove_all_clippaths(child)

        # --- 2. Process this node ---
        cp = self.get_clippath(node)
        if cp is None:
            return count

        # Convert shape to path (transform already flattened)
        geom = self.convert_to_path(node, Transform())
        if geom is None:
            # No geometry → remove clip-path and leave empty
            node.attrib.pop("clip-path", None)
            node.set("d", "")
            return count + 1

        # ClipPath must contain exactly one geometry child after flattening
        clip_geom = None
        for child in cp:
            if self.is_geometry(child, gt7_supported=True):
                clip_geom = child
                break

        if clip_geom is None:
            # No geometry → fully clipped
            node.attrib.pop("clip-path", None)
            node.set("d", "")
            return count + 1        
        
        # Boolean intersection
        clipped = self.path_intersection(geom, clip_geom)
        if clipped is None:
            node.attrib.pop("clip-path", None)
            node.set("d", "")
            return count + 1

        # Replace geometry
        node.set("d", clipped.get("d"))

        # Remove clip-path attribute
        node.attrib.pop("clip-path", None)

        return count + 1
    
    # endregion
    
    # region ---- Remove Pattern ----

    def pattern_mean_color(self, nodes):
        """
        Compute the mean RGB color of arbitrary SVG geometry.
        Uses:
        - rasterize_nodes(nodes)  → PNG bytes
        - Pillow + NumPy          → mean color
        Returns hex string "#RRGGBB".
        """

        # 1) Rasterize geometry into PNG bytes
        png_bytes = self.rasterize_nodes(nodes)

        # 2) Load PNG into Pillow
        img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        arr = np.array(img)

        # 3) Extract channels
        R = arr[:, :, 0].astype(np.float32)
        G = arr[:, :, 1].astype(np.float32)
        B = arr[:, :, 2].astype(np.float32)
        A = arr[:, :, 3].astype(np.float32) / 255.0

        # 4) Premultiply to handle transparency correctly
        Rm = R * A
        Gm = G * A
        Bm = B * A

        # 5) Mean alpha
        meanA = np.mean(A)
        if meanA == 0:
            return "#000000"  # fully transparent → treat as black

        # 6) Un‑premultiply
        meanR = np.mean(Rm) / meanA
        meanG = np.mean(Gm) / meanA
        meanB = np.mean(Bm) / meanA

        # 7) Clamp + convert to hex
        meanR = int(max(0, min(255, meanR)))
        meanG = int(max(0, min(255, meanG)))
        meanB = int(max(0, min(255, meanB)))

        return "#{:02X}{:02X}{:02X}".format(meanR, meanG, meanB)
    
    # endregion


    # region ---- Inkscape Actions ----

    def path_intersection(self, pathA, pathB):
        doc, ids = self.build_svg_for_actions([pathA, pathB], prefix="bool")

        root = doc.getroot()
        svg_input = inkex.etree.tostring(root, encoding="unicode")
        self.log(logging.DEBUG,f"Inkscape input:\n {svg_input}")

        select_ids = ",".join(ids)

        result_bytes = inkex.command.inkscape_command(
            doc,
            select=select_ids,
            actions="path-intersection",
        )

        result_doc = inkex.load_svg(result_bytes)
        result_root = result_doc.getroot()

        svg_output = inkex.etree.tostring(result_root, encoding="unicode")
        self.log(logging.DEBUG,f"Inkscape ouput:\n {svg_output}")

        out_paths = result_root.findall(".//{http://www.w3.org/2000/svg}path")
        candidates = [p for p in out_paths if p.get("id") not in ids]

        intersection = candidates[-1] if candidates else (out_paths[-1] if out_paths else None)
        if intersection is None:
            return self.empty_path()

        new_path = inkex.PathElement()
        new_path.set("d", intersection.get("d"))

        if hasattr(intersection, "path"):
            new_path.path = intersection.path

        self.copy_presentation_attributes(intersection, new_path)
        return new_path

    
    def path_union(self, paths):
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
            return None

        new_path = inkex.PathElement()
        new_path.set("d", union.get("d"))
        self.copy_presentation_attributes(union, new_path)
        return new_path


    def compute_tile_size_from_nodes(self, nodes):
        """
        Compute width & height from the bounding box of the nodes.
        This makes the rasterizer reusable for ANY geometry, not only patterns.
        """

        bbox = inkex.BoundingBox()
        for node in nodes:
            try:
                bbox += node.bounding_box()
            except Exception:
                pass  # non-geometry nodes

        width = bbox.width if bbox.width > 0 else 1.0
        height = bbox.height if bbox.height > 0 else 1.0

        return width, height


    def build_svg_for_actions(self, nodes, prefix):
        """
        Build a minimal standalone SVG document suitable for Actions API
        boolean operations. Works for intersection, union, difference, etc.
        """

        width, height = self.compute_tile_size_from_nodes(nodes)

        minimal_svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}"></svg>'
        )

        doc = inkex.load_svg(minimal_svg)
        root = doc.getroot()

        root.set('xmlns', 'http://www.w3.org/2000/svg')

        ids = []

        for i, node in enumerate(nodes):
            if not hasattr(node, "copy"):
                continue

            clone = node.copy()

            elem_id = f"{prefix}{i}"
            clone.set("id", elem_id)

            self.copy_presentation_attributes(node, clone)

            root.append(clone)

            # IMPORTANT: rebind using root, not doc
            clone = root.getElementById(elem_id)

            ids.append(elem_id)

        return doc, ids




    def rasterize_nodes(self, nodes):
            """
            Rasterize a list of inkex nodes using Inkscape's renderer via inkex actions API.
            Computes width & height automatically from node geometry.
            Returns PNG bytes.
            """

            # 1) Build SVG input
            doc, ids = self.build_svg_for_actions(nodes, "path")
            
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

            os.remove(tmp_png_path)

            self.log(logging.DEBUG,f"Read {len(png_bytes)} bytes")

            return png_bytes
    
    # endregion

# region --- Main ---

if __name__ == "__main__":
    GT7Export().run()

# endregion
