"""A small demo textbook (original text written for NovaQuest), made as a PDF so the demo school can upload it like
any school's own book. Two chapters with numbered sections."""
from __future__ import annotations

from pathlib import Path

TITLE = "NovaQuest Demo Science, Grade 8"
CHAPTERS = {
    (1, "Cells: The Building Blocks of Life"): {
        "1.1 What Is a Cell?": "Every living thing is made of cells. A cell is the smallest part of an organism that "
        "is alive and can carry out the work of life. Some organisms, such as bacteria and amoeba, are a single cell. "
        "Plants and animals are made of many millions of cells working together. Most cells are too small to see "
        "without a microscope. Robert Hooke first saw tiny boxes in a thin slice of cork and called them cells.",
        "1.2 Parts of a Cell": "A cell has three main parts. The cell membrane is a thin layer around the cell that "
        "controls what goes in and out. The cytoplasm is the jelly-like part where many activities of the cell take "
        "place. The nucleus controls the activities of the cell and holds the information passed on from parents. "
        "Small structures in the cytoplasm, such as mitochondria, have their own jobs.",
        "1.3 Plant and Animal Cells": "Plant cells have a cell wall outside the cell membrane. The cell wall is stiff "
        "and gives the plant support and protection. Plant cells also have green chloroplasts, which make food using "
        "sunlight, and a large vacuole filled with sap. Animal cells, such as the cells from the inside of our cheek, "
        "have no cell wall and no chloroplasts. Their vacuoles are small or absent.",
        "1.4 Shape and Size of Cells": "Cells come in many shapes and sizes, and the shape suits the job. A nerve cell "
        "is long and branched so it can carry messages around the body. Red blood cells are round and flat so they "
        "can squeeze through narrow blood vessels. The egg of a hen is a single cell that can be seen with the eye, "
        "while bacteria are so small that thousands fit across a millimetre."},
    (2, "Microorganisms: Friend and Foe"): {
        "2.1 Kinds of Microorganisms": "Microorganisms are living things too small to see without a microscope. The "
        "main groups are bacteria, fungi, protozoa and algae. Viruses are even smaller; they can only reproduce inside "
        "the cells of another living thing. Microorganisms live almost everywhere: in soil, water and air, and inside "
        "our bodies.",
        "2.2 Useful Microorganisms": "Many microorganisms help us. Bacteria called lactobacillus turn milk into curd. "
        "Yeast, a fungus, makes bread dough rise by giving out carbon dioxide gas. Some bacteria in the roots of pea "
        "and bean plants fix nitrogen from the air, which makes the soil more fertile. Antibiotics such as penicillin "
        "are made from microorganisms and kill disease-causing bacteria.",
        "2.3 Harmful Microorganisms": "Some microorganisms cause diseases. They are called pathogens. Cholera and "
        "typhoid are spread through dirty water, and tuberculosis through the air. Mosquitoes carry the protozoa that "
        "cause malaria. Microorganisms also spoil food, which is why we keep food cold, dry it, or add salt or sugar. "
        "Washing hands and drinking clean water stop many diseases from spreading."}}


MATHS_8_TITLE = "NovaQuest Demo Mathematics, Grade 8"
MATHS_8 = {
    (1, "Rational Numbers"): {
        "1.1 What Is a Rational Number?": "A rational number is a number that can be written as p/q, where p and q are "
        "integers and q is not zero. Every integer is a rational number, because 5 can be written as 5/1. Fractions such "
        "as 3/4 and -2/7 are rational numbers too. The number 1/0 is not defined, so q can never be zero.",
        "1.2 Adding and Subtracting Rational Numbers": "To add two rational numbers with the same denominator, add the "
        "numerators and keep the denominator. With different denominators, first write both with a common denominator, "
        "such as the least common multiple. For example, 1/3 + 1/4 = 4/12 + 3/12 = 7/12. Subtracting a number is the same "
        "as adding its additive inverse: 2/5 - 1/5 = 2/5 + (-1/5) = 1/5.",
        "1.3 Rational Numbers on the Number Line": "Every rational number has a place on the number line. To mark 3/4, "
        "divide the space between 0 and 1 into four equal parts and count three parts to the right of 0. Negative "
        "rational numbers lie to the left of 0. Between any two rational numbers there are always more rational numbers; "
        "the mean of two numbers lies between them."},
    (2, "Linear Equations in One Variable"): {
        "2.1 Solving Simple Equations": "An equation says that two expressions are equal, such as 2x + 3 = 11. A linear "
        "equation has the variable only to the power one. To solve it, do the same operation on both sides so the "
        "equation stays balanced: subtract 3 from both sides to get 2x = 8, then divide both sides by 2 to get x = 4. "
        "Check the answer by putting it back into the equation.",
        "2.2 Equations with the Variable on Both Sides": "Some equations have the variable on both sides, such as "
        "5x - 2 = 3x + 6. Move the variable terms to one side and the numbers to the other: subtract 3x from both sides "
        "to get 2x - 2 = 6, add 2 to get 2x = 8, so x = 4. Moving a term to the other side changes its sign.",
        "2.3 Word Problems": "Many everyday problems lead to linear equations. Read the problem, choose a letter for the "
        "unknown, and write an equation from the facts. If a number added to twice itself gives 36, then x + 2x = 36, so "
        "3x = 36 and x = 12. Always say what the answer means, such as 12 years or 12 rupees."},
    (3, "Understanding Quadrilaterals"): {
        "3.1 Polygons": "A polygon is a closed figure made of line segments that do not cross. A polygon with three sides "
        "is a triangle, with four sides a quadrilateral and with five sides a pentagon. A polygon is convex if no part of "
        "a line joining two points inside it goes outside. A regular polygon has all sides equal and all angles equal.",
        "3.2 Angle Sum of a Polygon": "The angles of a triangle add up to 180 degrees. A quadrilateral can be cut into two "
        "triangles by a diagonal, so its angles add up to 360 degrees. A polygon with n sides can be cut into n - 2 "
        "triangles, so its angles add up to (n - 2) x 180 degrees. The exterior angles of any convex polygon add up to "
        "360 degrees.",
        "3.3 Kinds of Quadrilaterals": "A parallelogram has both pairs of opposite sides parallel; its opposite sides and "
        "opposite angles are equal. A rectangle is a parallelogram with every angle a right angle. A rhombus has all four "
        "sides equal, and its diagonals cross at right angles. A square is both a rectangle and a rhombus. A trapezium has "
        "only one pair of parallel sides."}}

SCIENCE_9_TITLE = "NovaQuest Demo Science, Grade 9"
SCIENCE_9 = {
    (1, "Matter in Our Surroundings"): {
        "1.1 Matter Is Made of Particles": "Everything around us that has mass and takes up space is matter. Matter is "
        "made of very small particles. When salt dissolves in water the particles of salt spread out between the "
        "particles of water. The particles of matter have spaces between them, are always moving and attract each other.",
        "1.2 States of Matter": "Matter exists as solid, liquid or gas. A solid has a fixed shape and volume because its "
        "particles are packed closely and held strongly. A liquid has a fixed volume but takes the shape of its container. "
        "A gas has neither a fixed shape nor a fixed volume; its particles move freely and fast, so gases can be "
        "compressed easily.",
        "1.3 Evaporation": "Evaporation is the change of a liquid into vapour at any temperature below its boiling point. "
        "It happens faster when the surface area is larger, the temperature is higher, the air is drier and the wind is "
        "stronger. Evaporation causes cooling, which is why sweating cools our body and water in an earthen pot stays cool."},
    (2, "Motion"): {
        "2.1 Describing Motion": "An object is in motion when its position changes with time. We describe position "
        "from a reference point. Distance is the total length of the path travelled, while displacement is the shortest "
        "distance from the starting point to the end point in a given direction. Displacement can be zero even when the "
        "distance is not.",
        "2.2 Speed and Velocity": "Speed is the distance travelled in unit time, measured in metres per second. Average "
        "speed is the total distance divided by the total time. Velocity is speed in a given direction. An object moving "
        "in a circle at constant speed has a changing velocity, because its direction keeps changing.",
        "2.3 Acceleration": "Acceleration is the change in velocity per unit time. If a car's velocity changes from 10 m/s "
        "to 20 m/s in 5 seconds, its acceleration is (20 - 10) / 5 = 2 m/s squared. When velocity decreases the "
        "acceleration is negative; this is called retardation. A ball thrown up slows down because of the pull of gravity."},
    (3, "The Fundamental Unit of Life"): {
        "3.1 Discovery of the Cell": "Robert Hooke saw small compartments in a thin slice of cork in 1665 and called them "
        "cells. Later scientists found that all plants and animals are made of cells and that new cells come from cells "
        "that already exist. Some organisms, such as amoeba, are a single cell; others are made of many cells.",
        "3.2 Cell Organelles": "Inside a cell are organelles that do different jobs. The nucleus controls the cell and "
        "carries the chromosomes. Mitochondria release energy from food and are called the powerhouses of the cell. "
        "Ribosomes make proteins. Plant cells also have chloroplasts that make food using sunlight, and a cell wall made "
        "of cellulose.",
        "3.3 Cell Membrane and Transport": "The plasma membrane lets some substances in and out but not others, so it is "
        "called selectively permeable. Gases such as oxygen and carbon dioxide move across it by diffusion, from where "
        "they are more concentrated to where they are less. Water moves across it by osmosis. A cell placed in very salty "
        "water loses water and shrinks."}}


def make(path: Path, chapters: dict | None = None) -> Path:
    """Write a demo book to `path` (a PDF with selectable text): the Science 8 book unless other chapters are given."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    page = 1
    for (no, title), sections in (chapters or CHAPTERS).items():
        y = 790
        c.setFont("Helvetica-Bold", 18)
        c.drawString(60, y, f"Chapter {no}: {title}")
        y -= 40
        for head, body in sections.items():
            if y < 140:
                c.drawString(290, 30, str(page))
                c.showPage()
                page, y = page + 1, 790
            c.setFont("Helvetica-Bold", 12)
            c.drawString(60, y, head)
            y -= 20
            c.setFont("Helvetica", 11)
            line = ""
            for w in body.split():
                if len(line) + len(w) > 85:
                    c.drawString(60, y, line)
                    y, line = y - 15, ""
                    if y < 60:
                        c.drawString(290, 30, str(page))
                        c.showPage()
                        page, y = page + 1, 790
                        c.setFont("Helvetica", 11)
                line = f"{line} {w}".strip()
            if line:
                c.drawString(60, y, line)
                y -= 15
            y -= 18
        c.setFont("Helvetica", 9)
        c.drawString(290, 30, str(page))
        c.showPage()
        page += 1
    c.save()
    return path
