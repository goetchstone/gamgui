Classroom changes for example.com, from GamGUI's compare at 2026-11-11 18:00 UTC.

GamGUI did not make these changes and won't. You run them with GAM, outside GamGUI: they don't pass its preview,
its confirmation or its audit log. GamGUI's Classroom support hasn't been checked on a real school domain, and
neither have these commands. Try them on a test class first.

Before you start:
- Read review.csv: every change, one row each. It's safe to open in a spreadsheet.
- Never open the numbered files in a spreadsheet and save them: their values must stay exactly as written.
- Run the steps in order, from this folder, as an admin GAM is set up for. Each step finishes before the next.
- Classes are named by GamGUI's alias (d:gg-...), so later steps find a class created in step 3.

Steps:

01. Remove teachers who become students in the same class (1 row):
    gam csv 01-remove-teachers-changing-role.csv noescapechar true gam course '~course' remove teachers '~email'

02. Remove students who become teachers in the same class (1 row):
    gam csv 02-remove-students-changing-role.csv noescapechar true gam course '~course' remove students '~email'

03. Create classes, active, owned by their primary teacher (1 row):
    gam csv 03-create-classes.csv noescapechar true gam create course alias '~alias' name '~name' section '~section' teacher '~owner' state active

03. Create classes that have no section (1 row):
    gam csv 03-create-classes-without-section.csv noescapechar true gam create course alias '~alias' name '~name' teacher '~owner' state active

04. Rename classes; an empty section clears one (2 rows):
    gam csv 04-rename-classes.csv noescapechar true gam update course '~course' name '~name' section '~section'

05. Add teachers (1 row):
    gam csv 05-add-teachers.csv noescapechar true gam course '~course' add teachers '~email'

06. Add students (1 row):
    gam csv 06-add-students.csv noescapechar true gam course '~course' add students '~email'

07. Change owners; each new owner is already a teacher of the class (1 row):
    gam csv 07-change-owners.csv noescapechar true gam update course '~course' owner '~owner'

08. Remove teachers, a former owner once step 7 has run (1 row):
    gam csv 08-remove-teachers.csv noescapechar true gam course '~course' remove teachers '~email'

09. Add as students the former owners removed in step 8 (1 row):
    gam csv 09-add-former-owners-as-students.csv noescapechar true gam course '~course' add students '~email'

10. Remove students (1 row):
    gam csv 10-remove-students.csv noescapechar true gam course '~course' remove students '~email'

11. Archive classes (1 row):
    gam csv 11-archive-classes.csv noescapechar true gam update course '~course' state archived

Afterwards, open GamGUI's Plan an import and click Compare again: what's left is what didn't happen.
