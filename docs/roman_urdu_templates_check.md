# Roman Urdu jawab: check list

Client decision 21 (2026-10-20): developer in tayyar jawabon ko parh kar check kare.
Har line parho. Theek ho to `[x]` laga do. Badalna ho to 'Behtar jumla' mein naya jumla likh do; Claude use template file mein daal dega.
`{names}`, `{title}` jaise hisse bot khud bharta hai, inhein waise hi rehne do.

| # | Theek? | Kab bheja jata hai (key) | Abhi ka jumla (roman_ur) | Behtar jumla |
|---|---|---|---|---|
| 1 | [ ] | `ask_which_product` | Aap ka matlab kaun sa hai: {names}? | |
| 2 | [ ] | `ask_product_name` | Aap kaun si watch ke baare mein pooch rahe hain? Meherbani karke naam ya model bata dein. | |
| 3 | [ ] | `not_found` | Mujhe yeh product nahi mila. Meherbani karke naam ya model dobara check kar lein. | |
| 4 | [ ] | `ask_detail` | Maazrat, main samajh nahi saka. Aap thori tafseel se bata dein ke aap ko kya chahiye? | |
| 5 | [ ] | `ask_which_variant` | {title} in options mein milti hai: {options}. Aap ko kaun si chahiye? | |
| 6 | [ ] | `voice_unclear` | Maazrat, awaz saaf sunai nahi di. Meherbani karke dobara bhej dein ya likh kar bata dein. | |
| 7 | [ ] | `pictures_sent` | Ye lein: {title}. | |
| 8 | [ ] | `picture_colour_missing` | {title} ({colour} colour ki apni picture nahi hai, ye main picture hai) | |
| 9 | [ ] | `browse_list` | Ye watches aap ki pasand mein aati hain: {names}. In mein se kaun si achi lagi? Aap koi rang ya style bhi bata sakte hain. | |
| 10 | [ ] | `browse_none` | Is hisaab se koi watch nahi mili. Kya aap koi aur price, rang ya style try karna chahenge? | |
| 11 | [ ] | `language_switched` | Ji zaroor, ab main Roman Urdu mein jawab doon ga. Bataiye, main kya madad kar sakta hoon? | |
| 12 | [ ] | `private_data` | Main yahan order ya zaati maloomat share ya check nahi kar sakta. Hamari team is mein madad kar sakti hai. Kya main aap ki request unhein bhej doon? | |
| 13 | [ ] | `abuse_or_injection` | Maazrat, main is mein madad nahi kar sakta. Watches, price, delivery ya return ke baare mein zaroor poochein. | |
| 14 | [ ] | `out_of_scope` | Maazrat, main sirf watches, price, delivery aur return ke baare mein madad kar sakta hoon. | |
| 15 | [ ] | `handoff_created` | Maine aap ki request hamari team ko bhej di hai. Woh jald aap se yahin rabta karein ge. | |
| 16 | [ ] | `after_hours_note` | Hamari team ke auqaat {hours} hain. Woh wapas aate hi jawab dein ge. | |
| 17 | [ ] | `paused_ack` | Hamari team ke paas aap ki request hai, woh jald jawab dein ge. | |
| 18 | [ ] | `safe_fallback` | Maazrat, main abhi is ki tasdeeq nahi kar sakta. Maine hamari team ko madad ke liye bata diya hai. | |
| 19 | [ ] | `bot_trouble` | Maazrat, is waqt mujhe technical masla aa raha hai. Maine hamari team ko madad ke liye bata diya hai. | |
| 20 | [ ] | `voice_unavailable` | Maazrat, is waqt main voice message nahi sun sakta. Meherbani karke apna sawal likh kar bhej dein. | |
| 21 | [ ] | `shopify_unavailable` | Main abhi yeh check nahi kar sakta. Thori dair baad try karein, ya main team se madad ke liye keh sakta hoon. | |
| 22 | [ ] | `rate_limited` | Aap bohat tezi se messages bhej rahe hain. Thora intezar karein, phir dobara likhein. | |
| 23 | [ ] | `cooldown` | Main kuch dair jawab nahi de sakta. Baad mein dobara likhein, ya business hours mein hamari team madad karegi. | |
| 24 | [ ] | `card_warning` | Meherbani karke card ya payment ki tafseel is chat mein share na karein. Main inhein istemal nahi kar sakta aur chat mehfooz jagah nahi. Payment ke liye hamari team madad karegi. | |

Note (Claude): `cooldown` mein "business hours" likha hai, lekin client ki koi fix timing nahi (decision 18). Mashwara: "Thori dair baad dobara likhein." `after_hours_note` ab nahi bheja jayega (BUSINESS_HOURS=00:00-00:00).
