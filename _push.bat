@echo off
cd /d c:\Users\cxx\WorkBuddy\Claw\industrial_scada
git add -A
git commit -m "fix: alarm_output - buzzer pulse, manual toggle, flash thread safety"
git push origin main
