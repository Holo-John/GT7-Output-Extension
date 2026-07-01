#!/usr/bin/env python3
import traceback
import re
import copy
import inkex
from inkex.styles import Style
from inkex.transforms import Transform
import sys
import inspect
import os
import tempfile
import logging
import io

LOG_LEVEL = logging.DEBUG

class NullWriter:
    def write(self, *args, **kwargs):
        pass
    def flush(self):
        pass

class GT7Export(inkex.OutputExtension):
    """Save As → GT7 SVG"""
    
    STRIP_ALPHA_FROM_COLOR = True
    
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

    XLINK_NS = "http://www.w3.org/1999/xlink"

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

    def log(self, level, msg):
        """
        Unified logging wrapper:
        - Python logging (with real caller info)
        - inkex.utils.debug mirror
        """

        if level == logging.ERROR:
            self.logger.error(msg, stacklevel=2)
            self.msg(f"[ERROR] {msg}")
            
        elif level == logging.WARNING:
            self.logger.error(msg, stacklevel=2)
            self.msg(f"[WARNING] {msg}")

        elif level == logging.INFO:
            if self.logger.isEnabledFor(logging.INFO):
                self.logger.info(msg, stacklevel=2)

        elif level == logging.DEBUG:
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(msg, stacklevel=2)


    def add_arguments(self, pars):
        pass
        
    def effect(self):
        pass

    def save(self, stream):
        try:
            
            self.create_changelog(stream)
            
            self.log(logging.INFO, f"Python executable: {sys.executable}")
            self.log(logging.INFO, f"inkex loaded from: {inspect.getfile(inkex)}")
            self.log(logging.INFO, f"inkex version: {getattr(inkex, '__version__', 'NO VERSION ATTRIBUTE')}")

            self.preprocess(types_to_path=["text"], unlink_clones=True)
            
            self.resolve_styles_to_attributes()
            self.resolve_references()
            self.replace_unsupported_shapes()
            
            self.remove_all_groups()
            transform_count = self.apply_all_transforms()
            self.log(logging.INFO, f"Resolved {transform_count} transformations into plain geometry")
            
            self.translate_viewbox()
            self.clean_stroke_attributes()
            self.compress_output()
            self.cleanup_defs()

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

#--- NEW API ---

#--- DOM Tree and SVG Helpers ---

    def generate_id(self, el):
        """
        Generate a short ID: first letter of tag name (uppercase) + counter.
        Ensures uniqueness via svg.getElementById().
        """

        tag = self.tag_name(el) or "X"
        prefix = tag[0].upper()

        # counter per prefix
        count = self.id_counters.setdefault(prefix, 0)

        while True:
            count += 1
            candidate = f"{prefix}{count}"

            # ensure uniqueness in DOM
            if self.svg.getElementById(candidate) is None:
                break

        self.id_counters[prefix] = count
        el.set("id", candidate)
        return candidate

    
    def href_target(self, el):
        href = el.get("href") or el.get(f"{{{self.XLINK_NS}}}href")
        if not href or not href.startswith("#"):
            return None
        return self.svg.getElementById(href[1:])

    def parent_of(self, child):
        parent = child.getparent()
        if parent is None:
            return None, 0
        return parent, parent.index(child)

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

    def copy_presentation_attributes(self, src, dst):
        for attr in self.PRESENTATION_ATTRS:
            if attr in src.attrib:
                dst.set(attr, src.get(attr))

    def promote_presentation_attributes(self, clone, use_el):
        for attr in self.PRESENTATION_ATTRS:
            if attr in use_el.attrib and attr not in clone.attrib:
                clone.set(attr, use_el.get(attr))
                
    def parse_number(self, value):
        if value is None:
            return 0.0
        s = str(value).strip()
        if not s:
            return 0.0
        try:
            return float(s)
        except ValueError:
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

            
    def is_geometry(self, el):
        return self.tag_name(el) in (
            "path", "rect", "circle", "ellipse"
        )
        
    #--- Transformation Helpers ---    
        
        
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
                self.log(logging.ERROR, str(e))
                self.log(logging.ERROR, traceback.format_exc())
                pass

        return t_translate

    def append_transform(self, el, t):
        if t is None:
            return
            
        if not isinstance(el, inkex.ShapeElement):
            return
            
        try:
            base = Transform(el.get("transform") or "")
        except Exception:
            base = Transform()
            
        combined = t @ base
        el.set("transform", str(combined))
        
    def transform_path(self, node, transform):
        d = node.get("d")
        if d:
            p = inkex.Path(d) # type: ignore
            p = p.transform(transform)
            node.set("d", str(p))
            
    def transform_circle(self, node, transform):
        cx = float(node.get("cx", "0"))
        cy = float(node.get("cy", "0"))
        r  = float(node.get("r", "0"))

        (a, c, e), (b, d, f) = transform.matrix

        # Pure translation
        if a == 1 and d == 1 and b == 0 and c == 0:
            node.set("cx", str(cx + e))
            node.set("cy", str(cy + f))
            return

        # Uniform scale (with or without translation)
        if b == 0 and c == 0 and a == d:
            node.set("cx", str(cx * a + e))
            node.set("cy", str(cy * a + f))
            node.set("r",  str(r * abs(a)))
            return

        # Pure rotation around origin
        if a == d and b == -c:
            cx2 = a * cx + c * cy + e
            cy2 = b * cx + d * cy + f
            node.set("cx", str(cx2))
            node.set("cy", str(cy2))
            return

        # Anything else → circle becomes ellipse → convert to path
        self.circle_to_path(node, cx, cy, r, transform)
    
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
            return

        # Uniform scale (with or without translation)
        if b == 0 and c == 0 and a == d:
            node.set("x", str(x * a + e))
            node.set("y", str(y * a + f))
            node.set("width",  str(w * abs(a)))
            node.set("height", str(h * abs(a)))
            return

        # Anything else → becomes a path
        self.rect_to_path(node, x, y, w, h, transform)

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
            return

        # Uniform scale (with or without translation)
        if b == 0 and c == 0 and a == d:
            node.set("cx", str(cx * a + e))
            node.set("cy", str(cy * a + f))
            node.set("rx", str(rx * abs(a)))
            node.set("ry", str(ry * abs(a)))
            return

        # Anything else → becomes a path
        self.ellipse_to_path(node, cx, cy, rx, ry, transform)


    
#--- Resolve styles ---


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
            
#--- Resolve References ---

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
            
    def expand_use_once(self, use_el, stack):
        ref = self.href_target(use_el)
        if ref is None or not self.is_expandable_ref(ref):
            return False

        ref_id = ref.get("id")
        if ref_id and ref_id in stack:
            return False

        if ref_id:
            stack = set(stack)
            stack.add(ref_id)

        clone = copy.deepcopy(ref)
        self.remap_ids_in_clone(clone)

        self.promote_presentation_attributes(clone, use_el)

        clip_attr = use_el.get("clip-path")
        if clip_attr:
            clone.set("clip-path", clip_attr)

        for attr in ("href", f"{{{self.XLINK_NS}}}href", "x", "y"):
            clone.attrib.pop(attr, None)

        parent, idx = self.parent_of(use_el)

        wrapper = inkex.Group()
        self.add_node(wrapper, parent, idx)

        self.append_transform(wrapper, self.use_transform(use_el))

        self.add_node(clone, wrapper)

        self.remove_node(use_el, parent)

        return True

    def resolve_references(self):
        use_count = 0
        grad_count = 0

        root = self.svg
        changed = True
        stack = set()

        while changed:
            changed = False

            for el in list(root.iter()):
                tag = self.tag_name(el)

                match tag:
                    case "use":
                        if self.expand_use_once(el, stack):
                            changed = True
                            use_count += 1
                            break

                    case "path" | "rect" | "circle" | "ellipse":
                        grad_count += self.resolve_gradient_for_shape(el)

        if use_count:
            self.log(logging.INFO, f"Expanded {use_count} <use> elements")

        if grad_count:
            self.log(logging.INFO, f"Normalized {grad_count} gradients")
            

#--- Viewbox Translation ---

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

#--- Simplify Geometry ---

    def circle_to_path(self, node, cx, cy, r, transform):
        # Build path data for a circle using two arcs
        d = (
            f"M {cx - r},{cy} "
            f"a {r},{r} 0 1,0 {2*r},0 "
            f"a {r},{r} 0 1,0 {-2*r},0"
        )

        # Create path and apply transform
        p = inkex.Path(d).to_absolute().transform(transform) # type: ignore

        # Create new <path> element
        new_node = inkex.etree.Element(inkex.addNS("path", "svg"))
        new_node.set("d", str(p))

        # Copy presentation attributes (fill, stroke, etc.)
        self.copy_presentation_attributes(node, new_node)

        # Replace the circle in the DOM
        parent, idx = self.parent_of(node)
        self.remove_node(node)
        self.add_node(new_node, parent, idx)

        return new_node

    def rect_to_path(self, node, x, y, w, h, transform=None):
        rx = float(node.get("rx", "0") or "0")
        ry = float(node.get("ry", "0") or "0")

        # Clamp radii to valid range
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

        # Convert to inkex.Path and apply transform
        p = inkex.Path(d).to_absolute() # type: ignore
        if transform is not None:
            p = p.transform(transform)

        # Create new <path> element
        new_node = inkex.PathElement()
        new_node.set("d", str(p))

        # Copy presentation attributes (fill, stroke, etc.)
        self.copy_presentation_attributes(node, new_node)

        # Replace <rect> with <path> in DOM
        parent, idx = self.parent_of(node)
        self.remove_node(node)
        self.add_node(new_node, parent, idx)

        return new_node
        
    def ellipse_to_path(self, node, cx, cy, rx, ry, transform):
        # Build ellipse path using two arcs
        d = (
            f"M {cx - rx},{cy} "
            f"a {rx},{ry} 0 1,0 {2*rx},0 "
            f"a {rx},{ry} 0 1,0 {-2*rx},0"
        )

        # Convert to inkex.Path and apply transform
        p = inkex.Path(d).to_absolute().transform(transform) # type: ignore

        # Create new <path> element
        new_node = inkex.PathElement()
        new_node.set("d", str(p))

        # Copy presentation attributes (fill, stroke, etc.)
        self.copy_presentation_attributes(node, new_node)

        # Replace <ellipse> with <path> in DOM
        parent, idx = self.parent_of(node)
        self.remove_node(node)
        self.add_node(new_node, parent, idx)

        return new_node

    def poly_to_path(self, node, close=False):
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

        # Create new <path> element
        new_node = inkex.PathElement()
        new_node.set("d", " ".join(d))

        # Copy presentation attributes
        self.copy_presentation_attributes(node, new_node)

        # Replace original node
        parent, idx = self.parent_of(node)
        self.remove_node(node)
        self.add_node(new_node, parent, idx)

        return new_node

    def line_to_path(self, node):
        x1 = float(node.get("x1", "0"))
        y1 = float(node.get("y1", "0"))
        x2 = float(node.get("x2", "0"))
        y2 = float(node.get("y2", "0"))

        d = f"M {x1},{y1} L {x2},{y2}"

        # Create new <path> element
        new_node = inkex.PathElement()
        new_node.set("d", d)

        # Copy presentation attributes
        self.copy_presentation_attributes(node, new_node)

        # Replace original node
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
                        x = float(el.get("x", "0"))
                        y = float(el.get("y", "0"))
                        w = float(el.get("width", "0"))
                        h = float(el.get("height", "0"))
                        self.rect_to_path(el, x, y, w, h, transform=None)
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
            grad = self.svg.getElementById(grad_id)
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

    def apply_transform_to_node(self, node, transform):
        tag = self.tag_name(node)

        try:
            match tag:
                case "path":
                    self.transform_path(node, transform)

                case "circle":
                    self.transform_circle(node, transform)

                case "ellipse":
                    self.transform_ellipse(node, transform)

                case "rect":
                    self.transform_rect(node, transform)

                case _:
                    return 0

        except Exception as e:
            inkex.utils.debug(
                f"Node transform failed for id={node.get('id')} "
                f"tag={node.tag} attrib={dict(node.attrib)}"
            )
            inkex.utils.debug(f"exception: {type(e).__name__}: {e}")
            inkex.utils.debug(traceback.format_exc())
            return 0

        return 1
        
    def apply_all_transforms(self, node=None, parent_transform=None):
        # Modern inkex root access
        if node is None:
            node = self.svg

        if parent_transform is None:
            parent_transform = Transform()

        count = 0

        # Modern tag resolution
        tag = self.tag_name(node)

        # Skip non-geometry paint servers
        if tag in ("linearGradient", "radialGradient", "pattern",
                   "filter", "marker", "stop"):
            return count

        # Parse local transform safely
        try:
            local_transform = Transform(node.get("transform"))
        except Exception:
            local_transform = Transform()

        # Combine CTMs
        combined = parent_transform @ local_transform

        self.log(logging.DEBUG,
            f"[TYPE] parent={type(parent_transform)} "
            f"local={type(local_transform)} combined={type(combined)}"
        )

        self.log(logging.DEBUG,
            f"apply_all_transforms id={node.get('id')} "
            f"parent={parent_transform.matrix} "
            f"local={local_transform.matrix} "
            f"combined={combined.matrix}"
        )

        # Recurse into children
        for child in list(node):
            count += self.apply_all_transforms(child, combined)

        # Apply CTM to geometry
        count += self.apply_transform_to_node(node, combined)

        # Apply CTM to gradients referenced by this node
        count += self.apply_transform_to_gradients_used_by(node, combined)

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

#--- Cleanup <Defs> ----
        
    def collect_referenced_ids(self):
        referenced = set()

        # Modern inkex root access
        for el in self.svg.iter():
            for attr in self.REF_ATTRS:
                val = el.get(attr)
                if not val:
                    continue

                # url(#id)
                if val.startswith("url(#") and val.endswith(")"):
                    ref_id = val[5:-1]
                    referenced.add(ref_id)
                    self.log(logging.DEBUG,
                        f"[REF] url(#...) attr={attr} on id={el.get('id')} → {ref_id}"
                    )

                # bare #id
                if val.startswith("#"):
                    ref_id = val[1:]
                    referenced.add(ref_id)
                    self.log(logging.DEBUG,
                        f"[REF] bare #... attr={attr} on id={el.get('id')} → {ref_id}"
                    )

        self.log(logging.DEBUG, f"[REF] Final referenced ids: {sorted(referenced)}")
        return referenced
        
    def cleanup_defs(self):
        defs = self.svg.find(".//{http://www.w3.org/2000/svg}defs")
        if defs is None:
            return

        while True:
            referenced = self.collect_referenced_ids()
            removed_any = False

            for child in list(defs):
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

            if not removed_any:
                break



#--- Clean Attributes ---

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

#--- Strip Output ---

    def remove_inkscape_metadata(self):
        # Modern inkex root access
        root = self.svg

        # nsmap is immutable → cannot be modified directly
        # Instead, use your full namespace‑stripping pipeline
        self.strip_editor_namespaces_from_all_elements()


    def strip_editor_namespaces_from_all_elements(self):
        root = self.svg

        bad_ns = (
            "http://www.inkscape.org/namespaces/inkscape",
            "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd",
            "http://www.w3.org/1999/xlink",
        )

        for el in list(root.iter()):
            tag = self.tag_name(el)

            # Remove editor-only elements that break Inkex when namespace is stripped
            if tag in ("namedview", "guide"):
                parent = el.getparent()
                if parent is not None:
                    parent.remove(el)
                continue

            # Skip non-element nodes
            if not isinstance(el.tag, str):
                continue

            # Extract localname (strip namespace)
            if el.tag.startswith("{"):
                uri, local = el.tag[1:].split("}", 1)
            else:
                uri, local = None, el.tag

            # If the element is in a bad namespace → rewrite tag
            if uri in bad_ns:
                new_tag = local
            else:
                new_tag = el.tag if uri is None else f"{{{uri}}}{local}"

            # Build cleaned nsmap (remove bad namespaces)
            new_nsmap = {
                prefix: ns
                for prefix, ns in (el.nsmap or {}).items()
                if ns not in bad_ns
            }

            # Rebuild element
            new_el = inkex.etree.Element(new_tag, nsmap=new_nsmap)
            new_el.attrib.update(el.attrib)
            new_el[:] = el[:]

            parent = el.getparent()
            if parent is None:
                # DO NOT replace root — mutate it instead
                el.tag = new_tag
                el.attrib.clear()
                el.attrib.update(new_el.attrib)
                el[:] = new_el[:]
            else:
                parent.replace(el, new_el)


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

        for el in node.iter():

            # 1. Path data
            if "d" in el.attrib:
                el.set("d", self.round_floats_in_string(el.get("d")))

            # 2. Generic float attributes
            for attr in float_attrs:
                if attr in el.attrib:
                    el.set(attr, self.round_floats_in_string(el.get(attr)))

            # 3. Transform attributes (elements + gradients)
            if "transform" in el.attrib:
                el.set("transform", self.round_floats_in_string(el.get("transform")))

            if "gradientTransform" in el.attrib:
                el.set("gradientTransform", self.round_floats_in_string(el.get("gradientTransform")))

    def remove_editor_elements(self, node=None):
        if node is None:
            node = self.svg

        for el in list(node):
            tag = el.tag

            if not isinstance(tag, str):
                continue

            # Extract namespace + local
            if tag.startswith("{"):
                uri, local = tag[1:].split("}", 1)
            else:
                uri, local = None, tag

            # SPECIAL CASE: remove namedview entirely
            if local == "namedview" and uri in (
                "http://www.inkscape.org/namespaces/inkscape",
                "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd",
            ):
                node.remove(el)
                continue

            # Remove Inkscape/Sodipodi namespaced elements
            if uri in (
                "http://www.inkscape.org/namespaces/inkscape",
                "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd",
            ):
                node.remove(el)
                continue

            self.remove_editor_elements(el)


    def remove_editor_attributes(self, node=None):
        if node is None:
            node = self.svg

        for el in node.iter():
            tag = el.tag

            # Skip namedview entirely
            if isinstance(tag, str) and tag.endswith("namedview"):
                continue

            for attr in list(el.attrib.keys()):
                if attr.startswith("inkscape:") or attr.startswith("sodipodi:"):
                    del el.attrib[attr]
                    continue

                if ":" in attr:
                    del el.attrib[attr]
                    continue

                if attr == "xlink:href":
                    del el.attrib[attr]
                    continue

                if attr in ("transform-center-x", "transform-center-y"):
                    del el.attrib[attr]
                    continue

                if attr.startswith("tile-"):
                    del el.attrib[attr]
                    continue

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


    def compress_output(self):
        #self.remove_inkscape_metadata()
        #self.strip_editor_namespaces_from_all_elements()
        #self.remove_editor_elements()
        #self.remove_editor_attributes()
        self.remove_comments()
        self.group_by_common_presentation_attributes()
        self.round_all_coordinates()
        self.remove_redundant_attributes()
        
        self.log(logging.INFO, f"Compressed output")

##--- Resolving Gradients ---

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
        if not self.STRIP_ALPHA_FROM_COLOR:
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
                 f"(first+last only, alpha stripped={self.STRIP_ALPHA_FROM_COLOR})")

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

        # --- SPEC RULE 1: detect child transform ---
        child_gt = grad.get("gradientTransform")
        child_has_gt = bool(child_gt)

        inherited_gt = None

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

            # --- SPEC RULE 2: inherit only if child has NO gradientTransform ---
            if not child_has_gt:
                gt = g.get("gradientTransform")
                if gt and inherited_gt is None:
                    self.log(logging.DEBUG, f"Inheriting parent gradientTransform: {gt}")
                    inherited_gt = gt

            # Debug: child/parent chain
            self.log(logging.DEBUG, f"[CHAIN] Child gradient id={grad.get('id')}")

            # Follow href chain (both plain and xlink)
            href = g.get("href") or g.get(f"{{{self.XLINK_NS}}}href")
            if not href or not href.startswith("#"):
                self.log(logging.DEBUG, "    No href → chain ends here")
                break

            ref_id = href[1:]
            self.log(logging.DEBUG, f"[CHAIN] Child href → {ref_id}")

            ref = self.svg.getElementById(ref_id)
            if ref is None:
                self.log(logging.DEBUG, f"ERROR: referenced gradient {ref_id} not found")
                break

            self.log(logging.DEBUG, f"[CHAIN] Parent gradient found: id={ref.get('id')}")

            # Inherit attributes (but NEVER inherit gradientTransform or href/id)
            for attr, val in ref.attrib.items():
                if attr in ("id", "href", f"{{{self.XLINK_NS}}}href", "gradientTransform"):
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
        if child_has_gt:
            # Child overrides everything
            self.log(logging.DEBUG, f"  Child gradientTransform overrides parents: {child_gt}")
            T_chain = Transform(child_gt)

        elif inherited_gt:
            # Child has none → inherit first parent transform
            self.log(logging.DEBUG, f"  Using inherited parent gradientTransform: {inherited_gt}")
            T_chain = Transform(inherited_gt)

        else:
            # No transform anywhere → identity
            self.log(logging.DEBUG, "  No gradientTransform found → identity")
            T_chain = Transform()

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
            val = shape.get(attr)
            if not val or not val.startswith("url(#"):
                continue

            grad_id = val[5:-1]
            self.log(logging.DEBUG, f"Shape {shape.get('id')} uses gradient {grad_id}")

            grad = self.svg.getElementById(grad_id)
            if grad is None:
                self.log(logging.DEBUG, f"  ERROR: gradient {grad_id} not found in SVG tree")
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



if __name__ == "__main__":
    GT7Export().run()
