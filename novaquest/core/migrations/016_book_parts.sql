-- 1.25.0: a book that comes in parts (NCERT Ganita Prakash Grade 8 Part I and Part II). Each part is its own book (its
-- chapters keep the numbers printed in it); parts of the same school, board, subject and grade make one subject.
ALTER TABLE syllabi ADD COLUMN part smallint CHECK (part BETWEEN 1 AND 9);
