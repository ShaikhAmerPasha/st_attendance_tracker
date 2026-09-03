import os

with open('/home/ameer/Desktop/frappe-bench/apps/st_attendance_tracker/st_attendance_tracker/www/daily_checkin.html', 'r') as f:
    lines = f.readlines()

# find <style> and </style>
start1 = lines.index('<style>\n')
# We know the first part ends around 2614 in daily_checkin.html before the concise stuff starts, but let's just grab the whole thing up to "/* ── STATUS-COLORED SELECT DROPDOWN ── */" which was the start of phase 2.
style_lines = []
for i in range(start1, len(lines)):
    if '/* ── STATUS-COLORED SELECT DROPDOWN ── */' in lines[i] or '</style>' in lines[i]:
        break
    style_lines.append(lines[i])

style_lines.append("</style>\n")

with open('/home/ameer/Desktop/frappe-bench/apps/st_attendance_tracker/st_attendance_tracker/www/additional_work.html', 'r') as f:
    aw_lines = f.readlines()

start2 = aw_lines.index('<style>\n')
end2 = aw_lines.index('</style>\n')

new_aw = aw_lines[:start2] + style_lines + aw_lines[end2+1:]

with open('/home/ameer/Desktop/frappe-bench/apps/st_attendance_tracker/st_attendance_tracker/www/additional_work.html', 'w') as f:
    f.writelines(new_aw)

print("Replaced!")
