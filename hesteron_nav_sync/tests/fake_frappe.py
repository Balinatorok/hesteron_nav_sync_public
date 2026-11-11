"""
A tiny in-memory stand-in for the parts of `frappe` that matching.py / mapping.py use, so the
matching engine can be exercised without a Frappe site. Install with `install_fake()` before
importing the app modules. NOT a general frappe mock — only what these modules call.
"""

import sys
import types


class _Dict(dict):
	def __getattr__(self, k):
		try:
			return self[k]
		except KeyError:
			return None


def install_fake(tables: dict, item_has_helykod=True):
	fr = types.ModuleType("frappe")

	def _rows(doctype):
		return [_Dict(r) for r in tables.get(doctype, [])]

	def _match(row, filters):
		for k, v in (filters or {}).items():
			if isinstance(v, (list, tuple)):
				op, val = v
				if op == "in" and row.get(k) not in val:
					return False
				if op == "<" and not (row.get(k) or 0) < val:
					return False
				if op == "is" and val == "set" and not row.get(k):
					return False
			elif row.get(k) != v:
				return False
		return True

	def get_all(doctype, filters=None, fields=None, pluck=None, limit=None, **kw):
		out = []
		for r in _rows(doctype):
			if not _match(r, filters):
				continue
			if pluck:
				out.append(r.get(pluck))
			elif fields:
				d = _Dict()
				for f in fields:
					if " as " in f:
						src, alias = f.split(" as ")
						d[alias] = r.get(src)
					else:
						d[f] = r.get(f)
				out.append(d)
			else:
				out.append(r)
		return out[:limit] if limit else out

	class _DB:
		def exists(self, doctype, name):
			if isinstance(name, dict):
				return any(_match(r, name) for r in _rows(doctype))
			return any(r.get("name") == name for r in _rows(doctype))

		def get_value(self, doctype, filters, fieldname=None, as_dict=False):
			rows = [r for r in _rows(doctype) if (_match(r, filters) if isinstance(filters, dict) else r.get("name") == filters)]
			if not rows:
				return None
			r = rows[0]
			if isinstance(fieldname, list):
				return _Dict({f: r.get(f) for f in fieldname}) if as_dict else [r.get(f) for f in fieldname]
			return r.get(fieldname or "name")

		def sql(self, query, values=None, as_dict=False):
			# only the supplier-history query is issued; emulate it from PR/PI item tables
			dt = "Purchase Receipt Item" if "tabPurchase Receipt Item" in query else "Purchase Invoice Item"
			counts = {}
			for r in _rows(dt):
				if r.get("supplier") == values[0]:
					counts[r["item_code"]] = counts.get(r["item_code"], 0) + 1
			return [_Dict({"item_code": k, "c": v}) for k, v in counts.items()]

	class _Meta:
		def __init__(self, dt):
			self.dt = dt

		def has_field(self, f):
			return item_has_helykod if (self.dt == "Item" and f == "custom_helykód") else False

	fr.get_all = get_all
	fr.db = _DB()
	fr.get_meta = lambda dt: _Meta(dt)
	fr._ = lambda s: s
	fr.utils = types.ModuleType("frappe.utils")
	fr.utils.flt = lambda v, precision=None: float(v or 0)
	sys.modules["frappe"] = fr
	sys.modules["frappe.utils"] = fr.utils
	return fr
