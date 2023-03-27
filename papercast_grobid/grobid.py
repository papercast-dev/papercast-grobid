import logging
import string
import subprocess
import time
import urllib
from dataclasses import dataclass
from typing import Any, Dict, List

import numpy as np
import pdfplumber
from pdf2image import convert_from_path
from pdfplumber.page import CroppedPage
from scipdf import parse_pdf, parse_pdf_to_dict

from papercast.processors.base import BaseProcessor
from papercast.production import Production
from papercast.types import Author, PDFFile


@dataclass
class PDFBBox:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float


class GROBIDProcessor(BaseProcessor):
    def input_types(self) -> Dict[str, Any]:
        return {"pdf": PDFFile}

    def output_types(self) -> Dict[str, Any]:
        return {
            "title": str,
            "authors": List,
            "doi": str,
            "description": str,
            "abstract": str,
            "text": str,
            "figures": List[CroppedPage],
            "equations": List[CroppedPage],
        }

    def __init__(
        self,
        remove_non_printable_chars=True,
        serve_grobid_script="~/scipdf_parser/serve_grobid.sh",
        grobid_url="http://localhost:8070/",
    ):
        self.serve_grobid_script = serve_grobid_script
        self.remove_non_printable_chars = remove_non_printable_chars
        self.grobid_url = grobid_url
        self.grobid = None

        if self.serve_grobid_script is not None:
            while not self._grobid_online():
                logging.info("GROBID server not started, starting now...")
                self._start_grobid()

        self.init_logger()

    def __del__(self):
        if self.grobid is not None:
            self.grobid.terminate()

    def _extract(self, production: Production) -> Production:
        article_dict = parse_pdf_to_dict(str(production.pdf.path))
        if article_dict is None:
            raise Exception("Could not parse pdf")

        logging.info(f"Parsed pdf at {production.pdf.path}")

        metadata = {
            "outpath": str(production.pdf.path),
            "title": article_dict["title"],
            "authors": article_dict["authors"].split(";")
            if "authors" in article_dict
            else None,
            "doi": None,
            "arxiv_id": None,
            "description": article_dict["abstract"],
        }
        text = self._get_text_from_dict(article_dict)

        setattr(production, "metadata", metadata)
        setattr(production, "text", text)
        setattr(production, "article_dict", article_dict)
        return production

    def _extract_rich(self, production: Production) -> Production:
        article_soup = parse_pdf(str(production.pdf.path), soup=True)
        article_dict = parse_pdf_to_dict(str(production.pdf.path))

        authors = [
            Author(
                first_name=a.find("persname").find("forename").text,
                last_name=a.find("persname").find("surname").text,
                # email=a.find("email").text if a.find("email") else None,
            )
            for a in article_soup.find("teiheader").find_all("author")
        ]

        production.authors = authors
        production.title = article_dict["title"]
        # production = self._get_formula_figure_imgs(production, article_soup)
        text = self._get_text_from_dict(article_dict)
        production.abstract = article_dict["abstract"]
        production.text = text
        return production

    def _get_tei_obj_bbox(self, tei_obj):
        coords = tei_obj.get("coords").split(",")
        if not len(coords) == 5:
            self.logger.warning(
                f"Incorrect coordinate dimension for {tei_obj}, returning None"
            )
            return None
        try:
            coords = [float(x) for x in coords]
        except:
            self.logger.warning(f"Could not parse bbox for {tei_obj}, returning None")
            return None
        page, x0, y0, width, height = coords
        bbox = PDFBBox(
            int(page),
            float(np.round(x0)),
            float(np.round(y0)),
            float(np.round(x0)) + float(np.round(width)),
            float(np.round(y0)) + float(np.round(height)),
        )
        self.logger.info(f"Got bbox {bbox} for {tei_obj}")
        return bbox

    def _get_tei_obj_img(self, tei_obj, pdf_path, method="pdfplumber"):
        from copy import deepcopy

        bbox = self._get_tei_obj_bbox(tei_obj)
        if bbox is None:
            return None

        if method == "pdfplumber":
            pdf_obj = pdfplumber.open(pdf_path)
            pages = pdf_obj.pages
            page = pages[bbox.page - 1]
            image_bbox = (bbox.x0, bbox.y0, bbox.x1, bbox.y1)
            cropped_page = page.crop(image_bbox)
            cropped_page = cropped_page.to_image(resolution=300)

        elif method == "pdf2image":
            raise NotImplementedError
            pages = convert_from_path(pdf_path)
            page = pages[bbox.page - 1]
            image_bbox = (bbox.x0, bbox.y0, bbox.x1, bbox.y1)
            cropped_page = page.crop(image_bbox)

        else:
            raise NotImplementedError

        return cropped_page

    def _get_formula_figure_imgs(self, production, article_soup) -> Production:
        production.equations = []
        for formula in article_soup.find_all("formula"):
            formula_img = self._get_tei_obj_img(formula, production.pdf.path)
            production.equations.append(formula_img)

        production.figures = []
        for figure in article_soup.find_all("figure"):
            figure_img = self._get_tei_obj_img(figure, production.pdf.path)
            production.figures.append(figure_img)
        return production

    def process(self, input: Production, method=None, **kwargs) -> Production:

        # TODO move to base class
        for input_attr in self.input_types():
            if not hasattr(input, input_attr):
                raise AttributeError(
                    f"Input object {input} does not have attribute {input_attr}"
                )

        if method == "rich":
            return self._extract_rich(input)
        else:
            return self._extract(input)

    def _get_text_from_dict(self, article_dict):
        text_elements = [
            article_dict["title"],
            article_dict["abstract"],
            # article_dict["authors"],
        ]

        for section in article_dict["sections"]:
            text_elements.append(section["heading"])
            text_elements.append(section["text"])

        text = "\n\n".join(text_elements)

        if self.remove_non_printable_chars:
            printable = set(string.printable)
            text = "".join(filter(lambda x: x in printable, text))

        return text

    def _start_grobid(self):
        cmd = ["bash", "-c", self.serve_grobid_script]
        self.grobid = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        while not self._grobid_online():
            time.sleep(1)

    def _grobid_online(self):
        try:
            urllib.request.urlopen(self.grobid_url).getcode()  # type: ignore
            return True
        except:
            return False
