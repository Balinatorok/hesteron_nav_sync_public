"""
Small, deterministic lookups from NAV codes to ERPNext masters.

VAT:  NAV line VAT (percentage / exemption case) -> Tax Category name (document-level on the
      Purchase Invoice; every category needs its own Tax Rule).
UOM:  NAV UnitOfMeasureType enum -> ERPNext UOM. The default stock UOM for goods is "db".
"""

from __future__ import annotations

import frappe

# Tax Category names as they exist on the live site (afa-szamlazasi-modok-beallitasa.md,
# afa-adoszabalyok-adokategoriak.md). Only the purchase-side relevant ones are mapped.
TAX_CATEGORY_DOMESTIC_27 = "Belföldi 27%"
TAX_CATEGORY_TAM = "TAM"
TAX_CATEGORY_EU_GOODS = "EU termékbeszerzés (önadózás)"
TAX_CATEGORY_FOREIGN_SERVICE = "Külföldi szolgáltatás igénybevétele (37.§)"

# vatExemption/vatOutOfScope `case` codes (invoiceBase.xsd / spec §"VatRateType")
#   AAM  alanyi adómentes (supplier is a small-business exempt) — NOT the same as TAM
#   TAM  tárgyi adómentes (Áfa tv. 85-86.§)
#   KBAET/KBAUK  EU intra-community supply (we are the buyer -> self-assessed)
#   EAM  export outside EU
#   NAM  other exemption by international treaty etc.
#   ATK / EUFAD37 / EUFADE / EUE / HO  out-of-scope cases (place of supply abroad)
VAT_CASE_TO_TAX_CATEGORY = {
	"TAM": TAX_CATEGORY_TAM,
	"KBAET": TAX_CATEGORY_EU_GOODS,
	"EUFAD37": TAX_CATEGORY_FOREIGN_SERVICE,
	"EUFADE": TAX_CATEGORY_FOREIGN_SERVICE,
	# "AAM": deliberately unmapped — needs its own Tax Category (accountant decision), see README
}


def vat_label(vat: dict | None) -> str:
	"""Human label for the import line ("27%", "TAM", "FAD" ...)."""
	if not vat or not vat.get("kind"):
		return "?"
	if vat["kind"] in ("percentage", "content", "vatAmountMismatch") and vat.get("percentage") is not None:
		pct = vat["percentage"]
		return f"{round(pct * 100) if pct <= 1 else round(pct)}%"
	return vat.get("case") or vat["kind"]


def resolve_tax_category(vat: dict | None) -> tuple[str | None, bool]:
	"""Returns (tax_category or None, needs_review)."""
	if not vat or not vat.get("kind"):
		return None, True
	kind = vat["kind"]
	if kind in ("percentage", "content"):
		pct = vat.get("percentage") or 0.0
		pct = pct * 100 if pct <= 1 else pct
		if abs(pct - 27) < 0.01:
			return TAX_CATEGORY_DOMESTIC_27, False
		return None, True  # 5% / 18% / 0% rates: no dedicated category yet — human decides
	if kind == "vatDomesticReverseCharge":
		return None, True  # belföldi fordított adózás (Áfa tv. 142.§) — no category yet
	category = VAT_CASE_TO_TAX_CATEGORY.get(vat.get("case") or "")
	return category, category is None


# NAV UnitOfMeasureType -> ERPNext UOM name candidates, first existing one wins.
NAV_UOM_CANDIDATES = {
	"PIECE": ["db", "Nos", "Unit"],
	"KILOGRAM": ["kg", "Kg"],
	"TON": ["t", "Tonne"],
	"KWH": ["kWh"],
	"DAY": ["nap", "Day"],
	"HOUR": ["óra", "Hour"],
	"MINUTE": ["perc", "Minute"],
	"MONTH": ["hónap", "Month"],
	"LITER": ["l", "Litre", "Liter"],
	"KILOMETER": ["km", "Kilometer"],
	"CUBIC_METER": ["m3", "Cubic Meter"],
	"METER": ["m", "Meter"],
	"LINEAR_METER": ["fm", "m", "Meter"],
	"CARTON": ["karton", "Box", "Carton"],
	"PACK": ["csomag", "Pack", "Packet"],
}


def resolve_uom(nav_uom: str | None, nav_uom_own: str | None, item_stock_uom: str | None) -> str | None:
	"""
	Prefer the matched Item's stock UOM (that's what the receipt must be in). Otherwise translate
	the NAV enum to an existing ERPNext UOM; unknown -> None (caller leaves the field blank).
	"""
	if item_stock_uom:
		return item_stock_uom
	if nav_uom == "OWN" and nav_uom_own and frappe.db.exists("UOM", nav_uom_own):
		return nav_uom_own
	for candidate in NAV_UOM_CANDIDATES.get(nav_uom or "", []):
		if frappe.db.exists("UOM", candidate):
			return candidate
	return None
