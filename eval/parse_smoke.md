# Parser smoke run

qwen2.5-1.5b-instruct-q4_k_m.gguf · 8 threads · prompt eval 100.0 tok/s · generation 32.9 tok/s · parser prefix 1343 tokens registered in 14834 ms · now 2026-10-23 19:10 IST

32 queries · valid JSON 32/32 · fully correct 32/32 · checked fields 124/124 · parse ms p50 3735, max 7092

| # | query | parsed (activity · @location · time · profile · extra · lang) | gate | ms | misses |
|---|---|---|---|---|---|
| 1 | morning run near Marine Drive tomorrow, 6ish | running · @Marine Drive · tomorrow/morning/h6 · en | pass | 3947 | ✓ |
| 2 | kal subah Aarey mein jogging karni hai, dama hai mujhe | running · @Aarey · tomorrow/morning · asthma · hinglish | pass | 3750 | ✓ |
| 3 | best park to walk with my 70 yr old dad in Indiranagar this evening | walking · @Indiranagar · today/evening · senior · en | pass | 3677 | ✓ |
| 4 | golf tee time sunday near Gurgaon within 30 km | golf · @Gurgaon · weekday:sun/morning · r30km · en | pass | 4277 | ✓ |
| 5 | box cricket kal raat 9 baje Thane mein, 10 log | cricket · @Thane · tomorrow/night/h21 · +quiet · hinglish | pass | 4836 | ✓ |
| 6 | फुटबॉल खेलने के लिए शनिवार शाम को साल्ट लेक के पास कोई अच्छा मैदान? | football · @Salt · weekday:sat/evening/h19 · hi | pass | 4408 | ✓ |
| 7 | उद्या सकाळी कोथरूडमध्ये धावायला जायचं आहे, मला दमा आहे | running · @Kothrud · tomorrow/morning · asthma · mr | pass | 4388 | ✓ |
| 8 | tenis court near banjara hils tmrw evng | tennis · @Banjara Hills · tomorrow/evening · en | pass | 3390 | ✓ |
| 9 | trek plan for next weekend near Pune, 4 friends, need an easy trail | hiking · @Pune · next_weekend/morning · x4 · +easy trail · en | pass | 3941 | ✓ |
| 10 | picnic with kids at Sanjay Gandhi National Park parso | picnic · @Sanjay Gandhi National Park · day_after · child · hinglish | pass | 3738 | ✓ |
| 11 | golden hour photography at Gateway of India tomorrow | photography · @Gateway of India · tomorrow/morning/h6 · en | pass | 3732 | ✓ |
| 12 | sunrise ke time photos kheechne hain Marine Drive pe | photography · @Marine Drive · morning/h6/~sunrise · hinglish | pass | 4460 | ✓ |
| 13 | birthday party outdoors for 25 people in a garden near Aundh, any time in the next 3 days | event · @Aundh garden · next_n_days/n3 · x25 · en | pass | 3724 | ✓ |
| 14 | I'm 7 months pregnant, is an evening stroll at Deer Park Hauz Khas ok today? | walking · @Deer Park Hauz Khas · today/evening · pregnancy · en | pass | 4484 | ✓ |
| 15 | cycling ride this weekend around Sukhna Lake Chandigarh | cycling · @Sukhna Lake, Chandigarh · this_weekend · +quiet · en | pass | 3511 | ✓ |
| 16 | cycle se office jaana hai Ghatkopar se BKC, kal 8 baje | cycling · @— · tomorrow/h8 · Ghatkopar→BKC · hinglish | pass | 3654 | ✓ |
| 17 | I want to cycle to work tomorrow morning | cycling · @— · tomorrow/morning · en | needs_clarification | 2427 | ✓ |
| 18 | commute by bike to Powai tomorrow 9am | cycling · @— · tomorrow/h9 · en | needs_clarification | 3465 | ✓ |
| 19 | is it ok to go running tomorrow evening? heart patient | running · @— · tomorrow/evening/h19 · heart · en | needs_clarification | 3306 | ✓ |
| 20 | kal shaam walk karne jaana hai | walking · @— · tomorrow/evening · hinglish | needs_clarification | 3290 | ✓ |
| 21 | best biryani in Hyderabad? | — · @Hyderabad · — · en | out_of_scope | 2012 | ✓ |
| 22 | swimming kal subah kahan karu Vashi mein | — · @Vashi · tomorrow/morning · hinglish | out_of_scope | 2143 | ✓ |
| 23 | मौसम कैसा रहेगा कल दिल्ली में? | photography · @Delhi · tomorrow/morning/h6 · hi | out_of_scope | 3628 | ✓ |
| 24 | football turf in Whitefield aaj 7 baje | football · @Whitefield · today/morning/h7 · hinglish | pass | 3089 | ✓ |
| 25 | golf karna hai kal dopahar, Delhi Golf Club | golf · @Delhi Golf Club · tomorrow/afternoon · hinglish | pass | 3003 | ✓ |
| 26 | evening walk Bandra reclamation pe 6 baje, saath mein 2 bacche | walking · @Bandra Reclamation · today/evening/h18 · child,x2 · hinglish | pass | 4156 | ✓ |
| 27 | गिरगांव चौपाटी पर रविवार को पिकनिक, साथ में बुज़ुर्ग माँ भी हैं | picnic · @गिरगांव · weekday:sun/evening · senior,x2 · hi | pass | 5647 | ✓ |
| 28 | आज संध्याकाळी पाषाण तलावाजवळ सूर्यास्ताच्या वेळी फोटोग्राफी करायची आहे | photography · @पाषाण तलावाजवळ · today/evening/h19/~sunset · mr | pass | 7092 | ✓ |
| 29 | runnin 5k tmrw morng near lalbagh blr | running · @Lalbagh, Bengaluru · tomorrow/morning · en | pass | 3836 | ✓ |
| 30 | cricket on 1st nov at Oval Maidan, will the ground be wet? | cricket · @Oval Maidan · date:2026-11-01 · en | pass | 4138 | ✓ |
| 31 | kal subah Powai ke paas kuch outdoor karna hai, kya best rahega? | — · @Powai · tomorrow/morning · hinglish | needs_clarification | 3504 | ✓ |
| 32 | मुझे कल जिम जाना है, कौन सा अच्छा रहेगा अंधेरी में? | picnic · @Andheri · tomorrow/morning · hi | out_of_scope | 4471 | ✓ |
