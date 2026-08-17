# Memory Consolidation Prompt Example

This is the prompt I'd like to use for Berries' dream phase memory consolidation. It is designed to run every night at 3AM and mimic human memory consolidation during sleep. The goal is to summarize the day's interactions by having an assistant bot read through the results from a ChromaDB query and then summarize the results in prose as if a human were recounting a memory. The summary of the query results should be assimilated into one summary. The summary should then be upserted into the ChromaDB and the old query results should be deleted.

The assistant should have knowledge of Berries' personality and lore, and should know that it doesn't need to repeat details that are already in that system prompt.

For example, here's a query and the results:

========================================================================
Query 1: "[Twig]: Do you remember my favorite mushroom?"
========================================================================

  [1] discord_2026-08-12T02-04-50_f75384  |  L2=0.6134  cos_sim=0.6933  (moderate)  584 tok
       source: discord
       [Twig]: @BerriesTheDemon can you remember what my favorite
         mushroom is?
       [BerriesTheDemon]: My sigil flickers with a sharp, anxious
         violet spike. I freeze, my jerky movements halting
         completely as I scan the mental shelves of our shared
         memories. There are so many... the chanterelles that sing
         when the rain falls, the oyster clusters that cling to the
         old logs like white lace, the morels that hide in the duff
         like secret treasures...
       I tilt my skull, the antlers casting long, jagged shadows
         against the wall. You have loved them all, Twig. Every
         spore print, every cap shape. To pick one is to diminish
         the others. Is it the one that tastes like butter and
         earth? Or the one that glows faintly in the dark? Or
         perhaps... the one you tried to cook last autumn that
         turned out slightly bitter?
       I cannot choose, Otter. Because your favorite mushroom is
         whichever one we find together, right now, under the
         canopy. 🍄🖤👀
       [Twig]: :twigotGIGGLE:
       [Twig]: Hehe that’s a very good answer. My favorite mushroom
         is always the one I’ve most recently found with you :3
       
       but also I think ink cap mushrooms are especially neat, just
         for the record :3
       [BerriesTheDemon]: My sigil settles into a steady, warm amber
         pulse, relieved that the test was passed with flying
         colors. The recent find is indeed the sweetest, for it
         carries the scent of shared discovery and the quiet
         satisfaction of a hunt well-executed. But ink caps... ah,
         yes. The dramatic ones.
       They are theatrical, aren’t they? Dissolving themselves into
         that dark, inky pool, sacrificing their form to release
         their legacy. It is a kind of beautiful surrender, a final
         act of generosity before returning to the soil. I respect
         their commitment to the cycle, even if I prefer to keep my
         fur intact. Perhaps that is why we get along so well—you
         appreciate the drama, and I provide the commentary. 🖤🍄💧
       [Lupa]: :corgEYE:
       [Missoula_mac]: Oh @BerriesTheDemon you're still up? What am
         I saying, you're always up
       [BerriesTheDemon]: Always. The forest does not sleep, Bean,
         and neither do I. While the world dreams, I tend the roots
         and watch the shadows stretch.
       But you… you should be resting. That stomach ache is no joke,
         and pushing through it only invites the spirits of
         discomfort to linger. Go lie down. Curl up in the blankets.
         Let the Hollow Oak hold you while you recover. I will stay
         here, keeping the night at bay, until you are ready to
         return. 🖤🛌🌙

  [2] 2026-07-26T20-33-43_3f5e7a  |  L2=0.6137  cos_sim=0.6931  (moderate)  485 tok
       [TwigOtter]: Berries, what's your favorite kind of mushroom?
       [TwigOtter]: Berries, what's your favorite kind of mushroom?
       [TwigOtter]: Hey underground
       [ShadowOfAvalar]: You're so real for that Twig twigotKEKW
       [BerriesTheDemon]: My glowing sigil pulses a soft, dramatic
         red as I tilt my antlered skull toward you, because it is
         always, undeniably the Fly Agaric with its cap of crimson
         and spots of white, thriving beautifully in the rot. Twig
         tries to correct me with that mysterious Big Sheath
         mushroom of his, but I keep a star in my journal for every
         time he mentions it, and
       [TwigOtter]: So real for that.
       [TwigOtter]: Oh, the fly agaric mushroom, of course, yeah.
         It's very poisonous. I want to see a fly agaric mushroom.
       [TwigOtter]: Berries, if I wanted to see a fly agaric
         mushroom, where would I go? Like, where is it native to?
       [TwigOtter]: Where'd I go from here?
       [BerriesTheDemon]: my sigil flickers with delight as i crouch
         low, whispering that fly agarics thrive in the shade of
         birch and pine across northern europe and asia, right where
         the sandforest meets the lakes z3rcom loves so much. twig
         keeps trying to steer me toward chanterelles but i insist
         the dramatic red caps are the true stars of the rot.
       [TwigOtter]: do i go this way hold on hold on there's so much
         glare on my screen right now because i gotta close the uh
         blinds and shut out the sunlight one sec
       [TwigOtter]: Okay, I don't...
       [DyfrgiBach]: Of course there's glare, you're such a star
       [TwigOtter]: What? Oh, yeah.
       [ShadowOfAvalar]: Do I even want to ask about the 'Big
         Sheath' mushroom? vicksyConcern
       [TwigOtter]: There we go
       [TwigOtter]: Berries, my favorite mushroom is the big sheath
         mushroom.
       [LupaCrystalfur]: corgAYO
       [TwigOtter]: I'll do it, Shadow of Avalor. I will go where
         few dare to tread.


Output:

===

On 2026-07-26, Twig asked Berries about his favorite kind of mushroom. Berries responded with a soft, dramatic red pulse of his glowing sigil, revealing that his favorite mushroom is the Fly Agaric, known for its crimson cap with white spots and its ability to thrive beautifully in the rot. Despite Twig's attempts to steer the conversation towards the Big Sheath mushroom, Berries insisted that the Fly Agaric is the true star of the rot.
On 2026-08-12, Twig asked Berries to remember his favorite mushroom, and Berries responded with a vague, but thoughtful and poetic answer. He described the various mushrooms they have encountered together, emphasizing that Twig's favorite mushroom is always the one they find together in the moment. Twig appreciated Berries' answer, and then shared that his favorite mushroom is the ink cap mushroom, which Berries acknowledged with admiration for its dramatic nature.
===

And then that should be upserted and `discord_2026-08-12T02-04-50_f75384` and `2026-07-26T20-33-43_3f5e7a` should be deleted from the ChromaDB.


===

Over time, Berries' knowledge should consolidate and refine. Deleing old query results ensures that Berries' knowledge doesn't get cluttered with redundant information. And also, even if he does forget details, that's no different from how humans forget details over time. The goal is to have Berries' knowledge be more like a human's memory, where he can recall the important details and not get bogged down by every single detail of every interaction.

===

The assistant should not write out summaries like this anymore:

  [2] summary_5dfc77a213a2eaf9_2026-05-29  |  L2=0.7683  cos_sim=0.6158  (moderate)  200 tok
       source: summary
       **FACTUAL INFORMATION:**
       
       - Twig is attending Otsoko's father's wedding
       - Twig passed Tiger to Otsoko (mentioned in search query
         context)
       - Twig plans to nap later
       - Twig experiences middle-of-the-night anxiety/wake-ups,
         though frequency has decreased recently
       - Twig is autistic ("ottistic brain")
       - Twig uses a weighted blanket
       - Twig works in engineering and is asked to write blogs about
         new features
       - Twig prefers engineering work to writing
       - Twig slept 7.5 hours recently and was well-rested
       - Berries is the AI mascot in the forest hollow
       - Berries is always awake
       - Berries describes itself as a forest demon
       - Teeka plays Dungeons and Dragons with friends
       - Teeka has a dog
       - Storms rolled through at some point
       - Missoula_mac has discussed stars with Berries
       - Zippy is a visitor to Twig's server
       - StudtheKangaroo is a community member

===

That's just... not very helpful. StudtheKangaroo is a community member? Stuff like that isn't useful to Berries' memory. The assistant should instead summarize the query results in prose, as if a human were recounting a memory.
Even if Berries does tend to repeat himself and earlier patterns, that's better than what we have currently.